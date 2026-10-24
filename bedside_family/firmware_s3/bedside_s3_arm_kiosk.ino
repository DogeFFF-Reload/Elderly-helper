/* Mocen_CareMate · 床头盒固件 v4.6「屏键直控机械手」(嘉立创实战派 ESP32-S3)
 *
 * 目标效果：屏亮(!!!) + 六个中文大按钮，点一下 = 直接给机械手(arm_service:8013)发动作
 *
 * 显示引擎 = 立创厂家例程 esp32_s3_szp.c 的 esp_lcd 原生路径，一字不差照抄：
 *   ST7789 @ SPI3(80MHz, MODE2), MOSI=40 CLK=41 DC=39, 无RST脚
 *   CS 挂 PCA9557@0x19 (先 OUTPUT=0x05 / CONFIG=0xf8 → 拉低CS → panel_init)
 *   invert_color(true) + swap_xy(true) + mirror(true,false)
 *   背光 GPIO42 低电平亮
 *   ↑ 这是与此前所有尝试(Arduino_GFX/SPI库/ESP32_Display_Panel)不同的一条路
 *
 * v4.2 实锤：局部矩形刷新(100宽按压块)经过 esp_lcd 的 swap_xy+镜像坐标变换
 *   会错位到对角处且字节排布错乱 → 屏上"中心对称的按键糊掉"。
 * v4.6 修法：**全帧合成** —— 唯一画屏路径 = 320宽×20行整行块(从未出过的安全路径),
 *   按压高亮/顶部结果条/标题栏在线灯全部作为图层合进整帧再送。
 * 新增：标题栏右上常驻"在线灯" —— 绿=WiFi+服务全通 / 橙=WiFi在服务不通 / 红=离线
 *
 * 触摸 FT6336@0x38（v3.5 已跑通的原样复用；v4.5 加了垃圾帧过滤）
 * 网络：STA 连热点 → 直连 arm_service（笔记本:8013）：
 *      招手→/pose/wave 递药→/handover_med 递手机→/handover_item
 *      点赞→/pose/thumbs_up 张开手→/pose/fingers_5 归位→/reset
 *      MOCK 模式下 arm_service 只记日志不动手（安全）；真机动手要开 MOCK_SERIAL=0
 *
 * 烧录：compile --fqbn esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=huge_app
 *       upload -p COM7（原生JTAG口）;  日志看 COM8（CH340K→UART0, 115200）
 *
 * v5.1 留言版：点赞键 → 留言键。点一下=开始说（屏幕蓝条"正在留言"），再点一下=发送，
 *      最长 60 秒自动发送（服务端合成 WAV 落盘 + 网页收件箱）。
 * v5.5 听留言电流声修复：HTTP 分块奇数字节导致样本错位(白噪声)，跨块 1 字节
 *      进位对齐修复(详见 messagePlay 注释)。烧录后重测：App 发 fam_ 留言→屏播应清亮。
 * v5.6 WiFi 抗掉线：手机热点会踢省电设备，setAutoReconnect 卡死不回来
 *      (20261025 现场复现：屏掉线后按钮全"离线,没发")。三重加固=关 WiFi 省电
 *      + 按钮路径先自救重连 6s 再报错 + 15s 看门狗强制重连；掉线原因进串口日志。
 *
 * v5.2 留言收发版：按键表 [招手|手指操|猜拳 / 留言|听留言|语音]（归位仍在语音里说）。
 *      听留言=播最新留言/家属回话，没有则喇叭播"还没有"；
 *      ES8311+功放(PCA9557 PA_EN)开路。
 *      语音说"我想留言/说两句话"→自动进留言；"听留言/有没有回话"→自动播或播"还没有"。
 *
 * v5.3 播放修复版：spk_play_pcm 的 int16 成对写被编译器合并成 32-bit 宽存储，尾部
 *      越界一格砸中紧贴 .bss 的 s_spk_chan，句柄变样本值 0xfffefffe → 开机自检
 *      i2s_channel_write 处 LoadProhibited 死循环重启（按键全灭）。改 u32 单元素
 *      逐帧写 + 8 帧垫料；开机自检音撤下，播放由「听留言」首跑验证。
 *
 * v5.4 语义修正：听留言只播家属回话（fam_ 前缀，最新一条）——msg_ 是老人自己录的，
 *      归家属 app 收听；老人侧不播自己的留言。方向=老人「留言」→家属 app 听；
 *      家属 app 回话→老人「听留言」播。
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include "driver/spi_master.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "btn_bitmaps.h"
#include "voice_prompts.h"   /* 提示音(SAPI 16k/16bit/mono):还没有留言/已发送/开始录提示 */

/* ── DMA 完成回调：每画一块就等这个标记，杜绝缓冲复用竞态 ── */
static volatile bool s_tx_done = false;
static bool IRAM_ATTR on_tx_done(esp_lcd_panel_io_handle_t, esp_lcd_panel_io_event_data_t *, void *) {
  s_tx_done = true;
  return false;
}

/* 按压态位图显式地址表（v4.6 照片实锤：IMG_BTN_P0+i*8800 指针算术在 -fdata-sections
 * 下不保证落到 IMG_BTN_Pi —— 被按键糊块的真正根因，v4.0 起就存在） */
static const uint16_t* const BTN_PRESSED[6] = {IMG_BTN_P0, IMG_BTN_P1, IMG_BTN_P2, IMG_BTN_P3, IMG_BTN_P4, IMG_BTN_P5};

/* ══════════════ 现场校正区（烧现场版前只改这里） ══════════════ */
const char* WIFI_SSID = "Mocen_Bear";            // 现场热点(笔记本已连,同网)
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";;            // 从系统读出
const char* ARM_HOST  = "10.153.47.252";         // X5#A 手板(手#2) IP——静态已固化(agent 语音+真手版)
const int   ARM_PORT  = 8013;                    // arm_service 端口
/* ═════════════════════════════════════════════════════════════ */

/* ---- 实战派实配引脚（厂家 BSP 原值） ---- */
#define BSP_I2C_SDA 1
#define BSP_I2C_SCL 2
#define LCD_MOSI 40
#define LCD_SCLK 41
#define LCD_DC   39
#define LCD_BL   42          /* 低电平亮 */
#define BSP_LCD_SPI_NUM SPI3_HOST
#define BSP_LCD_PIXEL_CLOCK_HZ (80 * 1000 * 1000)
#define LCD_H_RES 320
#define LCD_V_RES 240
#define TP_ADDR 0x38
#define PCA_ADDR 0x19

/* 触摸映射（v4.9 定版:用户实测"按招手→归位被触发"实锤 v4.5-v4.8 翻转全反 */
#define TOUCH_FLIPX 0  /* lx = raw_y (不翻) */
#define TOUCH_FLIPY 1  /* ly = 239 - raw_x (翻) */
#define CAL_WINDOW_MS 300000UL  /* (v4.9 起校准窗已跳过,常数留档) */

/* ---- PCA9557（v3.5 已验证的读写） ---- */
static bool pca_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}
static bool pca_set_bit(uint8_t bit, uint8_t level) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write(0x01);                                  /* OUTPUT port */
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)PCA_ADDR, 1) < 1) return false;
  uint8_t cur = Wire.read();
  cur = level ? (cur | bit) : (cur & ~bit);
  return pca_write(0x01, cur);
}
static void pca9557_init(void) {                     /* 厂家原值 */
  pca_write(0x01, 0x05);                             /* DVP_PWDN=1 PA_EN=0 LCD_CS=1 */
  pca_write(0x03, 0xf8);                             /* bit0/1/2 输出 */
}
static void lcd_cs(uint8_t level) { pca_set_bit(0x01, level); }

/* ---- 显示（厂家 esp_lcd 路径） ---- */
static esp_lcd_panel_handle_t panel = NULL;
static uint16_t chunk[320 * 20];                     /* 12.8KB 搬运缓冲(整行20行) */

static bool lcd_init(void) {
  /* SPI 总线（字段顺序按本板包 spi_common.h 实际声明：mosi 在前 sclk 第三） */
  const spi_bus_config_t buscfg = {
    .mosi_io_num = LCD_MOSI,
    .miso_io_num = -1,
    .sclk_io_num = LCD_SCLK,
    .quadwp_io_num = -1,
    .quadhd_io_num = -1,
    .max_transfer_sz = LCD_H_RES * LCD_V_RES * sizeof(uint16_t),
  };
  if (spi_bus_initialize(BSP_LCD_SPI_NUM, &buscfg, SPI_DMA_CH_AUTO) != ESP_OK) {
    Serial.println("[lcd] spi_bus_initialize FAIL"); return false;
  }
  /* panel IO（字段顺序按本板包 esp_lcd_io_spi.h 实际声明：cs 在前 dc 第二） */
  const esp_lcd_panel_io_spi_config_t io_cfg = {
    .cs_gpio_num = -1,                    /* CS 由 PCA9557 管，不归 esp_lcd */
    .dc_gpio_num = LCD_DC,
    .spi_mode = 2,
    .pclk_hz = BSP_LCD_PIXEL_CLOCK_HZ,
    .trans_queue_depth = 1,   /* 1(=0轮询模式在此板包会触发xQueue断言崩溃) */
                              /* 竞态由 on_tx_done 回调+每笔等待解决 */
    .lcd_cmd_bits = 8,
    .lcd_param_bits = 8,
  };
  esp_lcd_panel_io_handle_t io = NULL;
  const esp_lcd_panel_io_callbacks_t io_cbs = {
    .on_color_trans_done = on_tx_done,
  };
  if (esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)BSP_LCD_SPI_NUM, &io_cfg, &io) != ESP_OK) {
    Serial.println("[lcd] new_panel_io FAIL"); return false;
  }
  esp_lcd_panel_io_register_event_callbacks(io, &io_cbs, NULL);
  /* ST7789 原生驱动 */
  const esp_lcd_panel_dev_config_t p_cfg = {
    .reset_gpio_num = -1,                 /* 无RST脚 */
    .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
    .bits_per_pixel = 16,
  };
  if (esp_lcd_new_panel_st7789(io, &p_cfg, &panel) != ESP_OK) {
    Serial.println("[lcd] new_panel_st7789 FAIL"); return false;
  }
  esp_lcd_panel_reset(panel);             /* 厂家顺序：reset(此时CS仍高,无害) */
  lcd_cs(0);                              /* 再拉低CS —— 厂家顺序原样 */
  if (esp_lcd_panel_init(panel) != ESP_OK) { Serial.println("[lcd] panel_init FAIL"); return false; }
  esp_lcd_panel_invert_color(panel, true);
  esp_lcd_panel_swap_xy(panel, true);
  esp_lcd_panel_mirror(panel, true, false);
  esp_lcd_panel_disp_on_off(panel, true);
  /* 背光全开（低电平亮） */
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, LOW);
  return true;
}

/* ═══════════ 全帧合成绘制（v4.6 唯一画屏路径） ═══════════
 * 图层自底向上：底图 IMG_IDLE → 顶部提示条(遮0..33行) → 按下键高亮块 → 标题栏在线灯
 * 所有 draw_bitmap 一律 320 宽整行窗口 —— 绝不再出现局部小窗口(对角糊块根因) */
static int8_t g_pressed = -1;    /* 按下高亮的按钮序号, -1=无 */
static int8_t g_strip = 0;       /* 顶部提示条 0=无 1=OK绿 2=ERR红 3=CAL 4=REC蓝 5=播放青 */
static int8_t g_chip = 0;        /* 在线灯 0=灰(未查) 1=绿(全通) 2=橙(WiFi在·服务不通) 3=红(离线) */

static const uint16_t* strip_src(int8_t k) {
  if (k == 1) return IMG_STRIP_OK;
  if (k == 2) return IMG_STRIP_ERR;
  if (k == 4) return IMG_STRIP_REC;
  if (k == 5) return IMG_STRIP_PLAY;
  return IMG_STRIP_CAL;
}

static void present(void) {
  for (int row0 = 0; row0 < 240; row0 += 20) {
    memcpy(chunk, IMG_IDLE + (size_t)row0 * 320, 320 * 20 * 2);   /* 底图 */
    if (g_strip && row0 < 34) {                                   /* 提示条层 */
      const uint16_t* s = strip_src(g_strip);
      int rows = (34 - row0 > 20) ? 20 : 34 - row0;
      for (int r = 0; r < rows; r++)
        memcpy(chunk + r * 320, s + (size_t)(row0 + r) * 320, 320 * 2);
    }
    if (g_pressed >= 0) {                                         /* 按下高亮层=原像素实时暗化
                                       (v4.7 实锤:预烤按压图裁剪错到对角区域,弃用) */
      int bx = BTN_X[g_pressed], by = BTN_Y[g_pressed];
      for (int r = 0; r < BTN_H; r++) {
        int ly = by + r;
        if (ly < row0 || ly >= row0 + 20) continue;
        uint16_t* line = chunk + (ly - row0) * 320 + bx;
        if (r < 2 || r >= BTN_H - 2) {            /* 白描边(0xFFFF 字节序对称,两序均白) */
          for (int x = 0; x < BTN_W; x++) line[x] = 0xFFFF;
        } else {
          line[0] = 0xFFFF; line[1] = 0xFFFF; line[BTN_W - 2] = 0xFFFF; line[BTN_W - 1] = 0xFFFF;
          for (int x = 2; x < BTN_W - 2; x++) {
            uint16_t v = line[x];
            uint16_t t = (uint16_t)((v >> 8) | (v << 8));        /* 还原真值(屏用换序字节) */
            uint16_t d = (uint16_t)(((((t >> 11) & 0x1F) * 55 / 100) << 11)
                                  | ((((t >> 5) & 0x3F) * 55 / 100) << 5)
                                  | ((t & 0x1F) * 55 / 100));    /* ×0.55 调暗 */
            line[x] = (uint16_t)((d >> 8) | (d << 8));           /* 回换序字节 */
          }
        }
      }
    }
    if (g_chip) {                                                 /* 在线灯层(标题栏右上) */
      static const uint16_t chipcol[4] = {
        /* 字节序预换(与生成器 SWAP_BYTES=1 对齐)：v4.6 实锤直接写 0xF800 屏上成青色 */
        0x0C63,   /* 灰=未查 */
        0xE007,   /* 绿=WiFi+服务全通 */
        0x20FD,   /* 橙=WiFi在·服务不通 */
        0x00F8,   /* 红=离线 */
      };
      uint16_t c = chipcol[g_chip];
      for (int r = 4; r < 23; r++) {
        if (r < row0 || r >= row0 + 20) continue;
        uint16_t* line = chunk + (r - row0) * 320;
        for (int x = 296; x < 317; x++) line[x] = c;
      }
    }
    s_tx_done = false;
    esp_lcd_panel_draw_bitmap(panel, 0, row0, 320, row0 + 20, chunk);
    uint32_t tw = millis();
    while (!s_tx_done && millis() - tw < 200) { /* 等这笔 DMA 真跑完再覆写缓冲 */ }
  }
}

/* ---- 触摸（v3.5 原样：FT6336@0x38 寄存器0x02 连读5字节；v4.5 垃圾帧过滤） ---- */
static bool getTouch(int16_t &tx, int16_t &ty) {
  Wire.beginTransmission(TP_ADDR);
  Wire.write(0x02);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)TP_ADDR, 5) < 5) return false;
  uint8_t d[5];
  for (int i = 0; i < 5; i++) d[i] = Wire.read();
  if (d[0] == 0 || d[0] > 2) return false;          /* 触点数要 1~2 */
  tx = ((d[1] & 0x0F) << 8) | d[2];
  ty = ((d[3] & 0x0F) << 8) | d[4];
  if (tx > 500 || ty > 500) return false;            /* 释放沿的垃圾帧(1024等)别当点击 */
  return true;
}

/* ---- 网络：直发 arm_service ---- */
static bool wifi_up() { return WiFi.status() == WL_CONNECTED; }

/* v5.6:掉线自救。手机热点(AP)会踢省电设备,setAutoReconnect 卡死时当场
   disconnect+begin 拉回;最多等 ms 毫秒,成功才放行发送/播放。 */
static bool net_wait(uint32_t ms) {
  if (wifi_up()) return true;
  Serial.println("[net] 掉线,尝试重连…");
  WiFi.disconnect(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    if (WiFi.status() == WL_CONNECTED) { Serial.println("[net] 重连成功"); return true; }
    delay(120);
  }
  Serial.println("[net] 重连失败");
  return wifi_up();
}

static void armSend(int i) {
  String url = String("http://") + ARM_HOST + ":" + String(ARM_PORT) + BTN_EP[i];
  if (!net_wait(6000)) {
    Serial.println("[net] 离线,没发");
    g_chip = 3; g_strip = 2; present();
    return;
  }
  HTTPClient http;
  http.begin(url);
  http.setConnectTimeout(3000);
  int code = http.POST("");
  http.end();
  Serial.print("[btn] "); Serial.print(BTN_EP[i]); Serial.print(" -> HTTP ");
  Serial.println(code);
  if (code >= 200 && code < 300) { g_chip = 1; g_strip = 1; }      /* 全通=绿,已发送√ */
  else if (code < 0)             { g_chip = 2; g_strip = 2; }      /* 服务连不上=橙 */
  else                           { g_chip = 1; g_strip = 2; }      /* 服务在但接口报错=绿+红条 */
  present();
}

/* ---- 语音：ES7210 录 3 秒 → 裸PCM POST → X5 离线识别 → 直接执行 ----
   寄存器序列逐条照抄乐鑫官方组件 es7210 v1.0.1（16kHz/MCLK=256fs/16bit/TDM）；
   引脚照立创实战派官方 wiki：I2S MCLK=38 BCK=14 WS=13 DIN=12，I2C 与触摸共用 Wire ---- */
#include "driver/i2s_tdm.h"
#include "driver/i2s_std.h"

#define MIC_I2S_MCK   38
#define MIC_I2S_BCK   14
#define MIC_I2S_WS    13
#define MIC_I2S_DIN   12
#define MIC_RATE      16000
#define MIC_RECORD_MS 3000

static i2s_chan_handle_t s_mic_chan = NULL;
static bool s_mic_ok = false;
static uint8_t s_mic_addr = 0;             /* 扫描得出(AD0/AD1 拉高决定,0x40..0x43) */

static bool es7210_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(s_mic_addr);
  Wire.write(reg); Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool es7210_probe() {
  const uint8_t cand[] = {0x40, 0x41, 0x42, 0x43};
  for (int i = 0; i < 4; i++) {
    Wire.beginTransmission(cand[i]);
    Wire.write(0x00);
    if (Wire.endTransmission() == 0) { s_mic_addr = cand[i]; return true; }
  }
  return false;
}

/* ES7210 全套初始化（抄 es7210_config_codec;16kHz 时钟表行:OSR=0x20/R02=0xC1/LRCK=0x01,0x00） */
static bool es7210_init() {
  bool ok = true;
  ok &= es7210_write(0x00, 0xFF);              /* 软复位 */
  ok &= es7210_write(0x00, 0x32);
  ok &= es7210_write(0x09, 0x30);              /* 上电状态周期 */
  ok &= es7210_write(0x0A, 0x30);
  ok &= es7210_write(0x23, 0x2A);              /* HPF ADC1/2 */
  ok &= es7210_write(0x22, 0x0A);
  ok &= es7210_write(0x21, 0x2A);              /* HPF ADC3/4 */
  ok &= es7210_write(0x20, 0x0A);
  ok &= es7210_write(0x11, 0x60);              /* SDP1: 16bit 标准 I2S */
  ok &= es7210_write(0x12, 0x02);              /* SDP2: 使能 1xFS TDM */
  ok &= es7210_write(0x40, 0xC3);              /* 模拟电源 */
  ok &= es7210_write(0x41, 0x70);              /* MIC1/2 偏置 2.87V */
  ok &= es7210_write(0x42, 0x70);              /* MIC3/4 偏置 2.87V */
  ok &= es7210_write(0x43, 0x1A);              /* MIC1 增益 30dB */
  ok &= es7210_write(0x44, 0x1A);              /* MIC2 */
  ok &= es7210_write(0x45, 0x1A);
  ok &= es7210_write(0x46, 0x1A);
  ok &= es7210_write(0x47, 0x08);              /* MIC1-4 上电 */
  ok &= es7210_write(0x48, 0x08);
  ok &= es7210_write(0x49, 0x08);
  ok &= es7210_write(0x4A, 0x08);
  ok &= es7210_write(0x07, 0x20);              /* OSR */
  ok &= es7210_write(0x02, 0xC1);              /* 16kHz 时钟分频(MCLK=256fs) */
  ok &= es7210_write(0x04, 0x01);              /* LRCK 分频高 */
  ok &= es7210_write(0x05, 0x00);              /* LRCK 分频低 */
  ok &= es7210_write(0x06, 0x04);              /* power down DLL */
  ok &= es7210_write(0x4B, 0x0F);              /* MIC1/2 偏置+ADC+PGA 上电 */
  ok &= es7210_write(0x4C, 0x0F);              /* MIC3/4 上电 */
  ok &= es7210_write(0x00, 0x71);              /* 使能 */
  ok &= es7210_write(0x00, 0x41);
  Serial.printf(ok ? "[mic] es7210@0x%02X config OK\n" : "[mic] es7210@0x%02X config FAIL\n", s_mic_addr);
  return ok;
}

static bool mic_init() {
  if (s_mic_ok) return true;
  if (!es7210_probe()) { Serial.println("[mic] 0x40-0x43 均无应答"); return false; }
  i2s_chan_config_t ch_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  ch_cfg.dma_desc_num = 12; ch_cfg.dma_frame_num = 240;
  if (i2s_new_channel(&ch_cfg, NULL, &s_mic_chan) != ESP_OK) { Serial.println("[mic] i2s chan FAIL"); return false; }
  i2s_tdm_config_t tdm = {
    .clk_cfg  = { .sample_rate_hz = MIC_RATE,
                  .clk_src = I2S_CLK_SRC_DEFAULT,
                  .mclk_multiple = I2S_MCLK_MULTIPLE_256 },
    .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                    I2S_SLOT_MODE_STEREO,
                                                    (i2s_tdm_slot_mask_t)(I2S_TDM_SLOT0 | I2S_TDM_SLOT1 |
                                                                          I2S_TDM_SLOT2 | I2S_TDM_SLOT3)),
    .gpio_cfg = { .mclk = (gpio_num_t)MIC_I2S_MCK, .bclk = (gpio_num_t)MIC_I2S_BCK,
                  .ws = (gpio_num_t)MIC_I2S_WS, .dout = (gpio_num_t)-1, .din = (gpio_num_t)MIC_I2S_DIN },
  };
  if (i2s_channel_init_tdm_mode(s_mic_chan, &tdm) != ESP_OK) { Serial.println("[mic] i2s tdm FAIL"); return false; }
  if (!es7210_init()) return false;         /* 先配 codec 再起时钟,顺序反了全是噪声 */
  if (i2s_channel_enable(s_mic_chan) != ESP_OK) { Serial.println("[mic] i2s enable FAIL"); return false; }
  s_mic_ok = true;
  return true;
}

/* 录 3 秒；TDM 4 槽:SLOT0=MIC1 SLOT1=MIC2(人声) SLOT2=MIC3(ES8311 回声,不用)。
   直接填 PCM 缓冲(16k 单声道 16bit),MIC1+MIC2 取均值,并打印各槽峰值供校准 */
static int mic_record_mono(uint8_t *buf, int buf_bytes) {
  int16_t *out = (int16_t *)buf;
  int cap = buf_bytes / sizeof(int16_t);
  int got = 0;
  size_t br = 0;
  int16_t raw[4 * 512];
  int16_t pk[4] = {0, 0, 0, 0};
  while (got < cap) {
    if (i2s_channel_read(s_mic_chan, raw, sizeof(raw), &br, pdMS_TO_TICKS(1500)) != ESP_OK) break;
    int n = br / (4 * sizeof(int16_t));
    if (n <= 0) break;
    for (int k = 0; k < n; k++) {
      for (int c = 0; c < 4; c++) {
        int16_t v = raw[k * 4 + c];
        if (v < 0) v = -v;
        if (v > pk[c]) pk[c] = v;
      }
      if (got < cap) out[got++] = ((int32_t)raw[k * 4 + 0] + raw[k * 4 + 1]) >> 1;
    }
  }
  Serial.printf("[mic] 槽峰值 MIC1=%d MIC2=%d MIC3=%d MIC4=%d\n", pk[0], pk[1], pk[2], pk[3]);
  return got * sizeof(int16_t);
}

/* 语音/留言共用的 3 秒采集缓冲(96KB,DRAM 内;两路径都不会并发) */
static uint8_t s_pcmbuf[48000 * 2];

/* ══════ 播放(v5.2):ES8311 DAC + 板载功放,播提示音和家属留言 ══════
 * 引脚共享:播放与录音同用 I2S0 的 MCLK38/BCK14/WS13,但**永不同时用**——
 * 放音前拆麦克风通道(spk_start→mic_teardown),之后 mic_init 会自动重建。
 * ES8311: I2C@0x18 slave I2S 格式;寄存器序列照乐鑫 es8311 组件源码抄。
 * MCLK=16k×256=4.096MHz(与麦克风同倍频);STD 立体声 16bit,DOUT=GPIO45。
 * 功放: PCA9557 PA_EN=BIT1,播放时才开(平时关,免底噪)。 */
#define SPK_I2S_DOUT 45
#define SPK_VOLUME   80                 /* 0~100,reg32=音量*256/100-1 */

static i2s_chan_handle_t s_spk_chan = NULL;

static bool es8311_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(0x18);
  Wire.write(reg); Wire.write(val);
  return Wire.endTransmission() == 0;
}

static void mic_teardown() {            /* 放音前拆录音通道(同口引脚只挂一个主人) */
  if (!s_mic_ok) return;
  i2s_channel_disable(s_mic_chan);
  i2s_del_channel(s_mic_chan);
  s_mic_chan = NULL; s_mic_ok = false;
}

static bool spk_start() {
  if (s_spk_chan) return true;          /* 已在播态(连播多条时不重复建) */
  mic_teardown();
  Wire.beginTransmission(0x18); Wire.write(0x00);
  if (Wire.endTransmission() != 0) { Serial.println("[spk] es8311@0x18 无应答"); return false; }
  i2s_chan_config_t ch_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  ch_cfg.auto_clear = true;
  if (i2s_new_channel(&ch_cfg, &s_spk_chan, NULL) != ESP_OK) { s_spk_chan = NULL; return false; }
  i2s_std_config_t std = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(16000),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = { .mclk = (gpio_num_t)MIC_I2S_MCK, .bclk = (gpio_num_t)MIC_I2S_BCK,
                  .ws = (gpio_num_t)MIC_I2S_WS, .dout = (gpio_num_t)SPK_I2S_DOUT, .din = (gpio_num_t)-1 },
  };
  std.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
  bool ok = (i2s_channel_init_std_mode(s_spk_chan, &std) == ESP_OK);
  /* ES8311 寄存器(组件 es8311.c 逐条对应;{MCLK 4096000, 16k, 16bit} 系数行) */
  ok &= es8311_write(0x00, 0x1F);       /* 软复位 */
  delay(20);
  ok &= es8311_write(0x00, 0x00);
  ok &= es8311_write(0x00, 0x80);       /* 上电(slave 串口默认) */
  ok &= es8311_write(0x01, 0x3F);       /* 全时钟开启,MCLK 取自引脚 */
  ok &= es8311_write(0x02, 0x00);       /* 16k@4.096M: pre_div=1 ×1 */
  ok &= es8311_write(0x03, 0x10);       /* ADC OSR */
  ok &= es8311_write(0x04, 0x10);       /* DAC OSR */
  ok &= es8311_write(0x05, 0x00);       /* adc/dac 分频 1 */
  ok &= es8311_write(0x06, 0x03);       /* BCLK 分频 */
  ok &= es8311_write(0x07, 0x00);       /* LRCK 分频高 */
  ok &= es8311_write(0x08, 0xFF);       /* LRCK 分频低 */
  ok &= es8311_write(0x00, 0x80);       /* slave I2S(fmt_config) */
  ok &= es8311_write(0x09, 0x0C);       /* SDP in 16bit */
  ok &= es8311_write(0x0A, 0x0C);       /* SDP out 16bit */
  ok &= es8311_write(0x0D, 0x01);       /* 模拟上电 */
  ok &= es8311_write(0x0E, 0x02);       /* PGA+ADC 调制器 */
  ok &= es8311_write(0x12, 0x00);       /* DAC 上电 */
  ok &= es8311_write(0x13, 0x10);       /* 耳机驱动输出 */
  ok &= es8311_write(0x14, 0x1A);       /* (与组件一致)模拟MIC+PGA */
  ok &= es8311_write(0x17, 0xC8);       /* ADC 增益 */
  ok &= es8311_write(0x1C, 0x6A);       /* ADC EQ 旁路/直流消除 */
  ok &= es8311_write(0x32, (SPK_VOLUME * 256 / 100) - 1);   /* 音量 */
  ok &= es8311_write(0x37, 0x08);       /* DAC EQ 旁路 */
  if (!ok) {
    Serial.println("[spk] es8311 配置失败");
    i2s_del_channel(s_spk_chan); s_spk_chan = NULL;
    return false;
  }
  if (i2s_channel_enable(s_spk_chan) != ESP_OK) {
    i2s_del_channel(s_spk_chan); s_spk_chan = NULL;
    return false;
  }
  pca_set_bit(0x02, 1);                 /* PA_EN=1 功放开 */
  Serial.println("[spk] es8311 OK, PA on");
  return true;
}

static void spk_stop() {
  pca_set_bit(0x02, 0);                 /* 功放先关,防末尾爆音 */
  if (!s_spk_chan) return;
  i2s_channel_disable(s_spk_chan);
  i2s_del_channel(s_spk_chan);
  s_spk_chan = NULL;
}

/* 16k/16bit 单声道 → 立体声同声输出。progmem=true 时指针指向 flash 数组。
   v5.3 写越界修复:旧版 int16 成对写 stereo[2k]/[2k+1],被编译器合并为 32-bit
   宽存储后尾部越界一格,正好砸中 .bss 里紧贴的 s_spk_chan(句柄被写成样本值
   0xfffefffe → i2s_channel_write 解引用 0xffff000a → LoadProhibited 死循环重启)。
   改为 u32 单元素逐帧写(每字节由一次性 32-bit 界内写覆盖)+ 数组尾部 8 帧垫料。 */
static bool spk_play_pcm(const uint8_t *pcm, int nbytes, bool progmem) {
  static uint32_t stereo32[2048 + 8];   /* 每帧=一个 u32(L/R 同值);尾 8 帧垫料 */
  if (!s_spk_chan || !pcm || nbytes < 2) return false;
  int ns = nbytes / 2;
  int done = 0;
  while (done < ns) {
    int take = (ns - done > 2048) ? 2048 : (ns - done);
    for (int k = 0; k < take; k++) {
      int idx = done + k;
      uint16_t s16 = progmem
        ? (uint16_t)(pgm_read_byte(pcm + (size_t)idx * 2)
                     | (pgm_read_byte(pcm + (size_t)idx * 2 + 1) << 8))
        : (uint16_t)pcm[idx * 2] | ((uint16_t)pcm[idx * 2 + 1] << 8);
      stereo32[k] = ((uint32_t)s16 << 16) | s16;
    }
    size_t wr = 0;
    if (i2s_channel_write(s_spk_chan, stereo32, (size_t)take * sizeof(uint32_t),
                          &wr, pdMS_TO_TICKS(600)) != ESP_OK) return false;
    done += take;
  }
  return true;
}

/* 播一条内置提示音后关功放 */
static void spk_prompt(const uint8_t *pcm, int len) {
  if (!spk_start()) return;
  spk_play_pcm(pcm, len, true);
  spk_stop();
}

/* 听留言(v5.4):拉留言列表 → 只播家属回话(fam_ 前缀,最新一条)。msg_=老人自己录的,
   归家属 app 收听,老人侧不播自己。没有家属回话则播"还没有"。
   播放中点屏任意处=停止。
   v5.5 电流声修复:HTTP 分块按到达时机切分,readBytes 可能返回奇数字节;
   spk_play_pcm 只保证"单次调用内"字节对齐,跨调用奇块让后续样本整体错位 8 位
   → 留言后半段全成白噪声(20261025 真机定位)。修法:1 字节进位缓存,
   悬空奇字节并入下一块,保证喂给 spk_play_pcm 的永远从头对齐、偶数字节。 */
static void messagePlay() {
  if (!net_wait(6000)) {
    Serial.println("[net] 离线,听不了留言");
    g_chip = 3; g_strip = 2; present();
    return;
  }
  String id = "";
  {
    HTTPClient h;
    h.begin(String("http://") + ARM_HOST + ":" + String(ARM_PORT) + "/messages");
    h.setConnectTimeout(2500);
    h.setTimeout(6000);
    int code = h.GET();
    String resp = h.getString();
    h.end();
    Serial.printf("[msg] list -> HTTP %d\n", code);
    if (code < 200 || code >= 300) { g_strip = 2; present(); delay(1200); g_strip = 0; present(); return; }
    /* v5.4:列表按时间倒序(msg_/fam_ 混排),只挑第一个 fam_ 家属回话 */
    int pos = 0;
    while (id.length() == 0) {
      int q = resp.indexOf("\"id\":", pos);
      if (q < 0) break;
      int a = resp.indexOf('"', q + 4) + 1, b = resp.indexOf('"', a);
      if (a <= 0 || b <= a) break;
      String cid = resp.substring(a, b);
      if (cid.startsWith("fam_")) id = cid;
      else pos = b;
    }
    if (id.length() == 0) {               /* 没有家属回话:播"还没有" */
      Serial.println("[msg] 信箱空(暂无家属回话)");
      spk_prompt(P_EMPTY_PCM, P_EMPTY_LEN);
      return;
    }
  }
  Serial.println("[msg] play " + id);
  if (!spk_start()) { g_strip = 2; present(); delay(1200); g_strip = 0; present(); return; }
  g_strip = 5; present();               /* 青条:正在播放留言 */
  HTTPClient h;
  h.begin(String("http://") + ARM_HOST + ":" + String(ARM_PORT) + "/messages/" + id + ".wav");
  h.setConnectTimeout(2500);
  h.setTimeout(10000);
  bool ok = false, stopped = false;
  int code = h.GET();
  Serial.printf("[msg] wav -> HTTP %d\n", code);
  if (code == 200) {
    WiFiClient *st = h.getStreamPtr();
    const int HDR = 44;                 /* 板上合成的 WAV 头恒定 44 字节 */
    uint8_t buf[2048];
    int skipped = 0;
    /* v5.5:跨块对齐。carry 是上一块剩下的悬空奇字节,并入本块头部,
       保证 (carry+got) 恒偶,样本边界永不漂移。 */
    uint8_t carry = 0;
    bool have_carry = false;
    ok = true;
    while (true) {
      int avail = st->available();
      if (avail <= 0) {
        if (!st->connected()) break;
        delay(2);
        continue;
      }
      int want = (avail < (int)(sizeof(buf) - (have_carry ? 1 : 0)))
                   ? avail : (int)(sizeof(buf) - (have_carry ? 1 : 0));
      int got = st->readBytes(buf + (have_carry ? 1 : 0), want);
      if (got <= 0) break;
      if (have_carry) { buf[0] = carry; got += 1; have_carry = false; }
      int base = 0;
      while (skipped < HDR) {          /* 头 44 字节:同样在字节流里数,不吃对齐亏 */
        int take2 = (got - base < HDR - skipped) ? (got - base) : (HDR - skipped);
        base += take2; skipped += take2;
        if (base >= got) break;
      }
      int nbytes = got - base;
      if (nbytes & 1) {                /* 奇数字节:尾字节悬空,留进位 */
        carry = buf[base + nbytes - 1];
        have_carry = true;
        nbytes -= 1;
      }
      if (nbytes > 0 && !spk_play_pcm(buf + base, nbytes, false)) { ok = false; break; }
      int16_t tx, ty;
      if (getTouch(tx, ty)) { stopped = true; break; }   /* 点屏即停 */
    }
  }
  h.end();
  spk_stop();
  g_strip = stopped ? 0 : 1;
  present();
  Serial.printf("[msg] play done ok=%d stop=%d\n", (int)ok, (int)stopped);
}

static void voiceSend() {
  if (!net_wait(6000)) {
    Serial.println("[net] 离线,语音没发");
    g_chip = 3; g_strip = 2; present();
    return;
  }
  if (!mic_init()) {
    Serial.println("[mic] init FAIL");
    g_chip = 1; g_strip = 2; present();
    return;
  }
  int nbytes = mic_record_mono(s_pcmbuf, sizeof(s_pcmbuf));
  Serial.print("[voice] 录音 "); Serial.print(nbytes); Serial.println(" bytes");
  if (nbytes < 8000) {                       /* 几乎没录到(codec 无数据)→红条 */
    g_chip = 1; g_strip = 2; present();
    return;
  }
  HTTPClient http;
  String url = String("http://") + ARM_HOST + ":" + String(ARM_PORT) + "/voice_cmd";
  http.begin(url);
  http.setConnectTimeout(3000);
  http.setTimeout(15000);
  int code = http.POST(s_pcmbuf, nbytes);
  String resp = http.getString();
  http.end();
  Serial.print("[voice] -> HTTP "); Serial.println(code);
  Serial.println(resp);
  bool ok = (code >= 200 && code < 300) &&
            (resp.indexOf("\"ok\":true") >= 0 || resp.indexOf("\"ok\": true") >= 0);
  if (ok && resp.indexOf("message_record") >= 0) {   /* 语音说想留言:播提示音后自动开录 */
    Serial.println("[voice] → 语音留言模式");
    g_chip = 1; g_strip = 0; present();
    spk_prompt(P_START_PCM, P_START_LEN);
    messageSend();
    return;
  }
  if (ok && resp.indexOf("message_play") >= 0) {     /* 听留言:板上已查到有货,直接播 */
    Serial.println("[voice] → 播放留言");
    g_chip = 1; g_strip = 0; present();
    messagePlay();
    return;
  }
  if (ok && resp.indexOf("message_none") >= 0) {     /* 还没有留言:播委婉提示 */
    Serial.println("[voice] → 暂无留言");
    g_chip = 1; g_strip = 0; present();
    spk_prompt(P_EMPTY_PCM, P_EMPTY_LEN);
    return;
  }
  if (ok)            { g_chip = 1; g_strip = 1; }   /* 听懂+已执行=已发送√ */
  else if (code < 0) { g_chip = 2; g_strip = 2; }   /* 服务连不上=橙 */
  else               { g_chip = 1; g_strip = 2; }   /* 服务在但没听懂=红条 */
  present();
}

/* ---- 留言(v5.1):点一下=开始(最长60s,3秒一段分块上传),再点一下=发送;点语音键=取消 ----
 * 缓冲复用 s_pcmbuf(96KB=3s@16k),边录边传,板上合成 WAV 落盘;屏上蓝条提示。 */
static int msg_chunk_post(const String &id, const uint8_t *buf, int nbytes) {
  HTTPClient h;
  h.begin(String("http://") + ARM_HOST + ":" + String(ARM_PORT) + "/message_chunk?id=" + id);
  h.setConnectTimeout(2000);
  h.setTimeout(6000);
  int code = h.POST((uint8_t *)buf, nbytes);
  h.end();
  return code;
}

static int msg_end_post(const String &id, bool cancel) {
  HTTPClient h;
  h.begin(String("http://") + ARM_HOST + ":" + String(ARM_PORT)
          + (cancel ? "/message_cancel?id=" : "/message_end?id=") + id);
  h.setConnectTimeout(2000);
  h.setTimeout(8000);
  int code = h.POST((uint8_t *)"", 0);
  h.end();
  return code;
}

/* 留言专用录音:边采边查触摸(32ms 粒度,I2S 每 512 帧一读)。
   hit: 0=无 1=留言键(该发送) 2=语音键(该取消);命中即提前返回已采字节(不足 3s 也照传)。 */
static int mic_record_msg(uint8_t *buf, int buf_bytes, int &hit) {
  int16_t *out = (int16_t *)buf;
  int cap = buf_bytes / sizeof(int16_t);
  int got = 0;
  size_t br = 0;
  int16_t raw[4 * 512];
  hit = 0;
  while (got < cap) {
    if (i2s_channel_read(s_mic_chan, raw, sizeof(raw), &br, pdMS_TO_TICKS(500)) != ESP_OK) break;
    int n = br / (4 * sizeof(int16_t));
    if (n <= 0) break;
    for (int k = 0; k < n; k++)
      if (got < cap) out[got++] = ((int32_t)raw[k * 4 + 0] + raw[k * 4 + 1]) >> 1;
    if (hit == 0) {
      int16_t tx, ty;
      if (getTouch(tx, ty)) {
        int lx = mapX(ty), ly = mapY(tx);
        if (lx >= BTN_X[3] && lx < BTN_X[3] + BTN_W && ly >= BTN_Y[3] && ly < BTN_Y[3] + BTN_H) hit = 1;
        else if (lx >= BTN_X[5] && lx < BTN_X[5] + BTN_W && ly >= BTN_Y[5] && ly < BTN_Y[5] + BTN_H) hit = 2;
        /* 点到空白/其他键:忽略,继续录 */
      }
    }
    if (hit) break;
  }
  return got * sizeof(int16_t);
}

static void messageSend() {
  if (!net_wait(6000)) {
    Serial.println("[net] 离线,留言没发");
    g_chip = 3; g_strip = 2; present();
    return;
  }
  if (!mic_init()) {
    Serial.println("[mic] init FAIL");
    g_chip = 1; g_strip = 2; present();
    return;
  }
  /* 先找板子要一个留言 id(/message_start) */
  String id = "";
  {
    HTTPClient h;
    h.begin(String("http://") + ARM_HOST + ":" + String(ARM_PORT) + "/message_start");
    h.setConnectTimeout(3000);
    h.setTimeout(6000);
    int code = h.POST((uint8_t *)"", 0);
    String resp = h.getString();
    h.end();
    int q = resp.indexOf("\"id\":");
    if (q >= 0) {
      int a = resp.indexOf('"', q + 5) + 1, b = resp.indexOf('"', a);
      if (a > 0 && b > a) id = resp.substring(a, b);
    }
    Serial.printf("[msg] start -> HTTP %d id=%s\n", code, id.c_str());
    if (code < 200 || code >= 300 || id.length() == 0) {
      g_chip = (code < 0) ? 2 : 1; g_strip = 2; present();
      delay(1200); g_strip = 0; present();
      return;
    }
  }
  g_strip = 4; present();  /* 蓝条:正在留言 */
  Serial.println("[msg] REC started");
  /* 等手指抬起(最多3s)再开始采第一段,防止起手触摸被当成"再按一下" */
  { int16_t tx, ty; uint32_t t0 = millis();
    while (getTouch(tx, ty) && millis() - t0 < 3000) delay(20); }
  int nseg = 0;
  bool cancelled = false, failed = false, done = false;
  for (; nseg < 20 && !done; nseg++) {              /* 20×3s = 60s 上限 */
    /* 边录边查触摸(32ms 粒度):再点留言键=发送,点语音键=取消,点空白=继续录 */
    int hit = 0;
    int nbytes = mic_record_msg(s_pcmbuf, sizeof(s_pcmbuf), hit);
    if (nbytes >= 1000) {
      int c2 = msg_chunk_post(id, s_pcmbuf, nbytes);   /* 提前打断时不足整段也照传 */
      Serial.printf("[msg] seg%d %dB -> HTTP %d\n", nseg + 1, nbytes, c2);
      if (c2 < 0) { g_chip = 2; failed = true; break; }
      if (c2 < 200 || c2 >= 300) { failed = true; break; }
    } else {
      Serial.println("[msg] mic 无数据"); failed = true; break;
    }
    if (hit == 1)      done = true;                    /* 再点留言键=发送 */
    else if (hit == 2) { cancelled = true; done = true; }  /* 语音键=取消 */
  }
  if (nseg >= 20) Serial.println("[msg] 60s 满,自动发送");  /* 满 60s 自动发送 */
  int c3 = -1;
  if (!failed) {
    c3 = msg_end_post(id, cancelled);
    Serial.printf("[msg] end (cancel=%d) -> HTTP %d\n", (int)cancelled, c3);
  }
  g_strip = 0;
  if (cancelled)      { ; }                         /* 已取消:静默回待机 */
  else if (failed)    { g_strip = 2; }
  else if (c3 >= 200 && c3 < 300) { g_strip = 1; g_chip = 1; spk_prompt(P_SENT_PCM, P_SENT_LEN); }
  else                { g_strip = 2; }
  present();                                        /* 结果条停一会,防连点 */
  delay(1200);
  g_strip = 0; present();
}

/* ---- setup ---- */
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] CareMate bedside_s3_arm_kiosk v5.6 留言收发+WiFi抗掉线");

  /* I2C 先行（PCA/触摸都在上面） */
  Wire.begin(BSP_I2C_SDA, BSP_I2C_SCL, 100000);
  delay(50);
  Serial.print("[i2c] scan:");
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Serial.print(" 0x"); Serial.print(a, HEX); }
  }
  Serial.println();

  pca9557_init();
  bool ok = lcd_init();
  Serial.println(ok ? "[lcd] init OK (esp_lcd ST7789 横屏320x240)" : "[lcd] init FAIL");
  if (ok) {
    g_chip = 3;          /* 红灯起步(还没联网) */
    g_strip = 0;
    present();
    Serial.println("[kiosk] 直进按键屏(校准已免)");
  } else {
    while (true) { delay(1000); Serial.println("[lcd] 显示初始化失败,停止"); }
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);   /* v5.6:关 WiFi 省电,手机热点环境防踢 */
  WiFi.onEvent([](WiFiEvent_t e, WiFiEventInfo_t i) {
    Serial.printf("[wifi] ev=%d rsn=%d\n", (int)e, (int)i.wifi_sta_disconnected.reason);
  });
  Serial.print("[wifi] 连接 "); Serial.println(WIFI_SSID);

  /* v5.3:开机自检音撤下——自检就是播放链路首跑,之前 spk_play_pcm 的写越界让它
     变成开机必炸(死循环重启,按键全死)。播放链路改由「听留言」键首跑验证。 */
}

/* ---- 触摸校准：聚类收集四角原始坐标(v4.9 起跳过,数据采到手会污染,弃用) ---- */
static int cal_k = 4;              /* 已收集的角数(=4=校准完) */
static long cal_sx = 0, cal_sy = 0, cal_sn = 0;
static int16_t cal_lx = -999, cal_ly = -999;  /* 当前聚类锚 */
static uint32_t cal_last = 0;
static bool cal_down = false;

static void cal_commit() {
  long cx = cal_sx / cal_sn, cy = cal_sy / cal_sn;
  Serial.print("[CAL] corner"); Serial.print(cal_k);
  Serial.print(" raw=(x:" );     Serial.print(cx);
  Serial.print(" y:");           Serial.print(cy);
  Serial.print(") n=");          Serial.println(cal_sn);
  cal_k++;
  cal_sx = cal_sy = cal_sn = 0;
  cal_lx = -999; cal_ly = -999;
}

static int16_t mapX(int16_t ry) {           /* v4.9 实测定版:lx = raw_y */
#if TOUCH_FLIPX
  return 319 - ry;
#else
  return ry;
#endif
}
static int16_t mapY(int16_t rx) {           /* v4.9 实测定版:ly = 239 - raw_x */
#if TOUCH_FLIPY
  return 239 - rx;
#else
  return rx;
#endif
}

/* ---- loop：触摸轮询 + 在线灯轮询 ---- */
static bool prevDown = false;
static uint32_t tLock = 0;
static uint32_t tChipChk = 0;
static uint32_t tCalEnd = 0;
static int lastChip = -1;

static void chipPoll() {
  int c = 3;
  if (wifi_up()) {
    HTTPClient h;
    h.begin(String("http://") + ARM_HOST + ":" + String(ARM_PORT) + "/status");
    h.setConnectTimeout(1500);
    h.setTimeout(1500);
    int code = h.GET();
    h.end();
    c = (code >= 200 && code < 300) ? 1 : 2;
  }
  if (c != lastChip) { lastChip = c; g_chip = c; present(); }
}

void loop() {
  /* v5.6 WiFi 看门狗:每 15s 查一次,断线就强制 begin 拉回 */
  static uint32_t tWifiWd = 0;
  if (millis() - tWifiWd > 15000) {
    tWifiWd = millis();
    if (!wifi_up()) {
      Serial.println("[wifi] 看门狗:断线,强制重连");
      WiFi.disconnect(false);
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
  }
  /* 校准窗口 */
  if (tCalEnd == 0) tCalEnd = millis() + CAL_WINDOW_MS;
  if (cal_k < 4 && millis() < tCalEnd) {
    int16_t rx, ry;
    bool down = getTouch(rx, ry);
    uint32_t now = millis();
    if (down) {
      if (!cal_down) {                       /* 新按下=新聚类 */
        if (cal_sn) cal_commit();
        cal_lx = rx; cal_ly = ry; cal_sn = 1; cal_sx = rx; cal_sy = ry;
      } else {
        int d = (rx - cal_lx > 0 ? rx - cal_lx : cal_lx - rx)
              + (ry - cal_ly > 0 ? ry - cal_ly : cal_ly - ry);
        if (d > 300) {                       /* 挪远了=新聚类 */
          cal_commit();
          cal_lx = rx; cal_ly = ry; cal_sn = 1; cal_sx = rx; cal_sy = ry;
        } else {
          cal_sx += rx; cal_sy += ry; cal_sn++;
          cal_lx = rx; cal_ly = ry;
        }
      }
      cal_last = now;
      cal_down = true;
    } else {
      if (cal_down && now - cal_last > 350 && cal_sn >= 5) cal_commit();
      if (cal_down && cal_sn > 0 && cal_sn < 5) { cal_sx=0; cal_sy=0; cal_sn=0; cal_lx=-999; cal_ly=-999; }
      cal_down = false;
    }
    if (cal_k >= 4) {
      Serial.println("[CAL] DONE 4/4 —— 校准数据齐,进按键屏");
      g_pressed = -1; g_strip = 1; present();   /* 绿条:校准完成 */
      delay(800);
      g_strip = 0; present();
    }
    return;                                   /* 校准期不跑按钮逻辑 */
  }
  if (cal_k < 4 && millis() >= tCalEnd) {
    Serial.println("[CAL] 超时退出(不满4角) —— 进按键屏");
    cal_k = 4;
    g_strip = 0; present();                   /* 撤掉校准条,亮主屏 */
  }

  /* 在线灯（标题栏右上，每8秒查一次：WiFi + arm_service 可达性） */
  if (millis() - tChipChk > 8000UL) {
    tChipChk = millis();
    chipPoll();
  }

  /* 触摸 */
  int16_t tx, ty;
  if (!getTouch(tx, ty)) { prevDown = false; return; }
  if (prevDown || millis() - tLock < 800) { return; }
  prevDown = true;
  tLock = millis();

  /* 映射 + 校准打印 */
  int lx = mapX(ty);
  int ly = mapY(tx);
  Serial.print("[tp] raw=(x:"); Serial.print(tx);
  Serial.print(" y:");          Serial.print(ty);
  Serial.print(") -> map=(");   Serial.print(lx);
  Serial.print(",");            Serial.print(ly);
  Serial.println(")");

  /* 按钮命中 */
  for (int i = 0; i < 6; i++) {
    if (lx >= BTN_X[i] && lx < BTN_X[i] + BTN_W && ly >= BTN_Y[i] && ly < BTN_Y[i] + BTN_H) {
      Serial.print("[tp] 按钮"); Serial.print(i); Serial.println(" 命中");
      g_pressed = i; g_strip = 0; present();   /* 按下高亮 */
      delay(120);
      if (i == 5) { voiceSend(); }             /* 语音键(挪到最后):录音→X5 识别→直接执行(内部画结果条) */
      else if (i == 3) { messageSend(); }      /* 留言键:点一下开录,再点一下发送(内部画结果条) */
      else if (i == 4) { messagePlay(); }      /* 听留言键(v5.2):播最新留言/回话,没有则播"还没有" */
      else { armSend(i); }                     /* 普通键:内部画结果条+在线灯 */
      delay(1200);                             /* 结果条停 1.2s，防连点 */
      g_pressed = -1; g_strip = 0; present();
      return;
    }
  }
  Serial.println("[tp] 没点到键上");
}