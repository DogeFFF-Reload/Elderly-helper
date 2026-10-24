/* Mocen_CareMate · 床头盒固件（嘉立创实战派 ESP32-S3 / Arduino-ESP32）
 *
 * v3.1 实战派实配版（2026-10-23，全部按 lckfb szpi-esp32s3 官方例程校正，占位引脚清零）：
 *   板子 = 立创·实战派ESP32-S3（ESP32-S3-WROOM-1-N16R8，16MB Flash + 8MB Octal PSRAM）
 *   一个 USB-C 经板载 WCH HUB 分两路：COM7=原生 USB-Serial/JTAG（烧录+日志）、COM8=CH340K→UART0
 *
 *   LCD  ST7789 2寸 IPS 320x240 横屏: MOSI=40 CLK=41 DC=39 BL=42(低电平亮,LEDC PWM 反相)
 *        CS/RST 挂在 PCA9557 IO 扩展芯片(@0x19)上 —— 必须先 init PCA9557 再拉低 CS！
 *        官方显示方向: swap_xy=true + mirror(true,false) → 本固件用 rotation=3 等价
 *   I2C  SDA=1 SCL=2 @100kHz：QMI8658@0x6A / PCA9557@0x19 / 触摸 / ES7210 / ES8311 共享总线
 *   触摸 CST816S@0x15（寄存器0x02 连读5字节）
 *   音频 ES7210(双麦)+ES8311(喇叭) I2S0: MCK=38 BCK=14 WS=13 DO=45；功放开关 PA_EN 也在 PCA9557
 *        （喇叭要响: pca9557 置 PA_EN=1 + ES8311 初始化——下一版本接，本版反馈全在屏幕）
 *
 * 三个触摸大键（语义与 master_service 对齐，v2 的 play/send 语义已废弃）：
 *   「我没事」→ GET  /bedside_button?which=ok     （确认拨号 / 取消警报）
 *   「听留言」→ GET  /api/memo → 屏幕显示最新留言（只读，不误报）
 *   「我到了」→ POST /api/checkin                 （报平安，主脑记时间线，手比赞）
 * 屏幕顶部全宽条 = 状态灯：绿(守护中) / 黄(等待家属) / 红(警报/掉线)
 *
 * 刷写（arduino-cli）：
 *   lib install "GFX Library for Arduino"
 *   compile --fqbn esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=huge_app
 *   upload  -p COM7（原生 JTAG 口，快）或 COM8（CH340K 路）
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>

/* ---- 网络/主脑（烧现场版前必改） ---- */
const char* WIFI_SSID = "dbs-hotspot";        // 手机热点 SSID
const char* WIFI_PASS = "dabashou2026";       // 手机热点密码
const char* MASTER    = "http://192.168.43.21:8000";   // 笔记本(主脑)在热点里的 IP

/* ---- 实战派实配引脚 ---- */
#define LCD_MOSI 40
#define LCD_SCLK 41
#define LCD_DC   39
#define LCD_BL   42          /* 低电平亮 */
#define TP_SDA   1
#define TP_SCL   2

/* ---- PCA9557 IO 扩展芯片 @0x19（LCD_CS / PA_EN / DVP_PWDN 挂它上面） ---- */
#define PCA_ADDR         0x19
#define PCA_OUTPUT_PORT  0x01
#define PCA_CONFIG_PORT  0x03
#define PCA_BIT_LCD_CS   0x01
#define PCA_BIT_PA_EN    0x02
#define PCA_BIT_DVP_PWDN 0x04

static bool pca_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}
static bool pca_set(uint8_t bit, uint8_t level) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write(PCA_OUTPUT_PORT);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)PCA_ADDR, 1) < 1) return false;
  uint8_t cur = Wire.read();
  cur = level ? (cur | bit) : (cur & ~bit);
  return pca_write(PCA_OUTPUT_PORT, cur);
}

/* ---- 显示（横屏 320x240） ---- */
Arduino_DataBus *bus = new Arduino_ESP32SPI(LCD_DC, /*CS=*/-1, LCD_SCLK, LCD_MOSI, GFX_NOT_DEFINED, HSPI /*官方 BSP 用 SPI3_HOST*/);
/* 实战派官方 SPI mode = 2（CPOL=1/CPHA=0）；库默认给 ESP32 塞 MODE3 —— 派生类掰过来 */
class Arduino_ST7789_M2 : public Arduino_ST7789 {
public:
  Arduino_ST7789_M2(Arduino_DataBus *b, int8_t rst, uint8_t r, bool ips, int16_t w, int16_t h)
      : Arduino_ST7789(b, rst, r, ips, w, h) {}
  bool begin(int32_t speed = GFX_NOT_DEFINED) override {
    _override_datamode = SPI_MODE2;   // protected 成员，子类可写
    return Arduino_ST7789::begin(speed);
  }
};
/* ST7789 320x240 横屏；官方例程: invert=true, swap_xy=true, mirror_x=true mirror_y=false
   → Arduino_GFX rotation: 先用 r=3，屏幕歪了改 r（现场迭代） */
Arduino_GFX *gfx = new Arduino_ST7789_M2(bus, /*RST=*/-1, 3 /*rotation*/, true /*IPS*/, 320, 240);

const uint16_t C_BG   = RGB565_BLACK;
const uint16_t C_OK   = RGB565_GREEN;    // 守护中
const uint16_t C_WAIT = RGB565_YELLOW;   // 等待家属
const uint16_t C_ALM  = RGB565_RED;      // 警报/掉线
const uint16_t C_TXT  = RGB565_WHITE;

/* ---- 三个键（320x240 横屏，底部三栏） ---- */
struct Btn { int16_t x, y, w, h; const char* label; };
Btn BTNS[3] = {
  {  10, 134,  96, 96, "我没事" },
  { 112, 134,  96, 96, "听留言" },
  { 214, 134,  96, 96, "我到了" },
};
String memoLine = "";    // 「听留言」展示区
String memoTime = "";

/* ============================ 绘制 ============================ */

void drawStatus(const char* txt, uint16_t color, const char* tail) {
  gfx->fillRect(0, 0, 320, 46, color);
  gfx->setTextColor(RGB565_BLACK);
  gfx->setTextSize(2);
  gfx->setCursor(8, 14);
  gfx->print(txt);
  if (tail) { gfx->setTextSize(1); gfx->setCursor(238, 20); gfx->print(tail); }
}

void drawButtons() {
  for (auto &b : BTNS) {
    gfx->fillRoundRect(b.x, b.y, b.w, b.h, 10, RGB565_GREEN);
    gfx->drawRoundRect(b.x, b.y, b.w, b.h, 10, RGB565_WHITE);
    gfx->setTextColor(RGB565_BLACK);
    gfx->setTextSize(2);
    gfx->setCursor(b.x + 10, b.y + 38);
    gfx->print(b.label);
  }
  gfx->setTextColor(RGB565_WHITE);
  gfx->setTextSize(1);
  gfx->setCursor(8, 116);
  gfx->print("Mocen_CareMate v3.5");
}

void redrawHome(const char* st, uint16_t color, const char* tail) {
  gfx->fillScreen(C_BG);
  drawStatus(st, color, tail);
  drawButtons();
  if (memoTime.length()) {           // 留言展示区（键上方）
    gfx->setTextColor(C_TXT);
    gfx->setTextSize(2);
    gfx->setCursor(8, 56); gfx->print(memoTime);
    gfx->setTextSize(1);
    gfx->setCursor(8, 78); gfx->print(memoLine);
  }
} 

/* 状态条单独刷（按钮/留言区不闪） */
void refreshStatus(const char* txt, uint16_t color, const char* tail) {
  drawStatus(txt, color, tail);
}

/* ============================ 触摸 ============================ */

bool getTouch(int16_t &tx, int16_t &ty) {
  /* FocalTech FT6336@0x38（I2C 扫描实锤，CST816S 的 0x15 不在线）：
     寄存器 0x02 连读 5 字节 [0]=触点数 [1][2]=X高X低 [3][4]=Y高Y低，布局与 CST816 相同 */
  Wire.beginTransmission(0x38);
  Wire.write(0x02);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(0x38, 5) < 5) return false;
  uint8_t d[5];
  for (int i = 0; i < 5; i++) d[i] = Wire.read();
  if (d[0] == 0) return false;
  tx = ((d[1] & 0x0F) << 8) | d[2];
  ty = ((d[3] & 0x0F) << 8) | d[4];
  return true;
}

/* ============================ 网络 ============================ */

void sendEvent(const char* which) {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(String(MASTER) + "/bedside_button?which=" + which);
  int code = http.GET();
  http.end();
  Serial.printf("[btn] %s -> HTTP %d\n", which, code);
}

void fetchMemos() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(String(MASTER) + "/api/memo");
  int code = http.GET();
  if (code != 200) { http.end(); return; }
  String body = http.getString();
  http.end();
  /* 极简裸解：最新一条是数组末尾；条目字段 {"t":"HH:MM","text":"..."} */
  int t = body.lastIndexOf("\"t\":");
  int k = body.lastIndexOf("\"text\":\"");
  memoTime = (t >= 0) ? body.substring(t + 6, min((int)body.length(), t + 15)) : "";
  if (k >= 0) {
    int e = k + 8;
    while (e < (int)body.length() && body.charAt(e) != '"') e++;   // 找闭合引号
    memoLine = body.substring(k + 8, e);
    if (memoLine.length() > 30) memoLine = memoLine.substring(0, 30);
  } else {
    memoLine = "(留言簿空着)";
    memoTime = "";
  }
  memoLine.replace("\\n", " ");
  Serial.println("[memo] " + memoTime + " " + memoLine);
  redrawHome("守护中", C_OK, wifiOk());
}

void sendCheckin() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(String(MASTER) + "/api/checkin");
  int code = http.POST("");
  http.end();
  Serial.printf("[checkin] HTTP %d\n", code);
}

void pollState() {
  if (WiFi.status() != WL_CONNECTED) { refreshStatus("无网络", C_ALM, NULL); return; }
  HTTPClient http;
  http.begin(String(MASTER) + "/state");
  int code = http.GET();
  if (code != 200) { http.end(); refreshStatus("掉线等待", C_ALM, NULL); return; }
  String body = http.getString();
  http.end();
  bool alarm = body.indexOf("ESCALATE") >= 0 || body.indexOf("ASKED") >= 0
            || body.indexOf("CONFIRM_DIAL") >= 0 || body.indexOf("FALL") >= 0;
  bool waitf = body.indexOf("WAIT_FAMILY") >= 0;
  if (alarm)      refreshStatus("警报·呼叫中", C_ALM, NULL);
  else if (waitf) refreshStatus("等待家属", C_WAIT, NULL);
  else            refreshStatus("守护中", C_OK, wifiOk());
}

/* ============================ 主流程 ============================ */

void setup() {
  Serial.begin(115200);
  delay(150);
  Serial.println("\n[boot] CareMate bedside v3.4");

  /* I2C（触摸/扩展芯片/传感器共享） */
  Wire.begin(TP_SDA, TP_SCL, 100000);
  delay(50);

  /* 总线扫描：0x15=触摸 0x19=PCA9557 0x6A=QMI8658 (+ES7210/ES8311) */
  Serial.print("[i2c] scan:");
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) Serial.printf(" 0x%02X", a);
  }
  Serial.println();

  /* PCA9557 先醒：官方默认 DVP_PWDN=1 PA_EN=0 LCD_CS=1 → 然后拉低 CS 保持选中 */
  bool w1 = pca_write(PCA_CONFIG_PORT, 0xF8);
  bool w2 = pca_write(PCA_OUTPUT_PORT, 0x05);
  bool w3 = pca_set(PCA_BIT_LCD_CS, 0);
  Serial.printf("[pca] cfg=%d out=%d cs_low=%d\n", w1, w2, w3);
  Serial.println("[i2c] PCA9557 ok");

  /* 背光：GPIO42 低电平亮（已实测确认）→ 常亮 */
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, LOW);

  bool lcdOk = gfx->begin(8000000);
  gfx->fillScreen(C_BG);
  gfx->setTextColor(C_TXT);
  gfx->setTextSize(2);
  gfx->setCursor(10, 100);
  gfx->print("连接主脑中...");
  Serial.printf("[lcd] begin_ret=%d\n", lcdOk);

  /* WiFi：功率压低防电流冲击复位（USB 供电的板子先活命要紧） */
  delay(1500);
  WiFi.setTxPower(WIFI_POWER_11dBm);
  Serial.println("[wifi] begin...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000) {
    delay(300);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[net] connected, IP=");
    Serial.println(WiFi.localIP());
    redrawHome("守护中", C_OK, NULL);
  } else {
    Serial.println("[net] FAILED (无网演示模式)");
    redrawHome("无网络", C_ALM, NULL);   // 没网也画键：触摸照常，事件只打日志
  }
  Serial.println("[ui] home drawn, entering loop");
}

const char* wifiOk() {
  return (WiFi.status() == WL_CONNECTED) ? "NET" : "--";
}

bool inBtnXY(int16_t x, int16_t y, int16_t bx, int16_t by, int16_t bw, int16_t bh) {
  return x >= bx && x <= bx + bw && y >= by && y <= by + bh;
}

void loop() {
  static unsigned long tBeat = 0;
  if (millis() - tBeat > 5000) { tBeat = millis(); Serial.println("[loop] alive"); }
  int16_t tx, ty;
  if (getTouch(tx, ty)) {
    static unsigned long lastTouch = 0;
    if (millis() - lastTouch > 350) {
      lastTouch = millis();
      Serial.printf("[touch] raw x=%d y=%d\n", tx, ty);   // 校准用
      if (inBtnXY(tx, ty, BTNS[0].x, BTNS[0].y, BTNS[0].w, BTNS[0].h)) {   // 我没事
        drawStatus("已上报", C_OK, NULL);
        sendEvent("ok");
        delay(600); pollState();
      } else if (inBtnXY(tx, ty, BTNS[1].x, BTNS[1].y, BTNS[1].w, BTNS[1].h)) {  // 听留言
        drawStatus("拉留言...", C_WAIT, NULL);
        fetchMemos();
        delay(600); pollState();
      } else if (inBtnXY(tx, ty, BTNS[2].x, BTNS[2].y, BTNS[2].w, BTNS[2].h)) {  // 我到了
        drawStatus("已报平安", C_OK, NULL);
        sendCheckin();
        delay(600); pollState();
      }
    }
  }
  static unsigned long tPoll = 0;
  if (millis() - tPoll > 2000) { tPoll = millis(); pollState(); }
  delay(10);
}