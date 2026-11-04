/**
 * Mocen_CareMate 床头屏B · 涂鸦 T5AI-Board（板+LCD 堆叠）
 *
 * 角色（10/25 定版）：
 *   T5AI = 大床头屏 → 控制 X5-B（臂 + 手#1）的 arm_service（code/hand_controller/arm_service.py）
 *   （嘉立创 S3 = 小床头屏 → 控手#2 那块板的 arm_service，固件另烧）
 *
 * 版本 v0.4：
 *   · 中文位图按钮（../gen_t5ai_btns.py 单源生成 t5ai_bitmaps.h；LVGL 默认字体无汉字且离线装不了转换器）
 *   · 触摸→**异步** HTTP：LVGL 线程永不阻塞（v0.2 同步 POST 卡死 UI 的教训），
 *     回调只上反馈+挂 pending，loop() 里发请求
 *   · 顶部状态条位图：红=未连接 / 绿=已连接 / 橙=听音中（2s 轮询 /status）
 *   · 语音：点语音键录 3 秒（Audio 库：16k/16bit/单声道 PCM，与 X5 vosk 同规格）
 *     → POST /voice_cmd（application/octet-stream）→ X5 关键词识别直接执行
 *     （与 S3 小屏同一后端链路，X5 侧零改动）
 *   · 面板原生 320x480 竖（ILI9488 RGB，板级宏 BOARD_LCD_ROTATION=0），
 *     UI 旋转 90 跑横屏；触摸坐标 LVGL indev 层自动补偿（lv_indev.c:675），
 *     旋转渲染由 lv_vendor disp_flush 的 lv_draw_sw_rotate 承接
 *
 * 烧录：USB 接 UART0（CH342 A 口），arduino-cli fqbn = tuya_open:tuya_open:TUYA_T5AI_BOARD
 *       串口日志在 COM9（A 口），B 口（COM10）乱码不读
 * 授权：占位码可正常启动 SDK（实测无断言）；接涂鸦云时才需要真码
 */
#include "Log.h"

#include "tuya_cloud_types.h"
#include "tal_api.h"

#include "board_com_api.h"
#include "lvgl.h"
#include "lv_vendor.h"
#include <WiFi.h>            /* TuyaOpen WiFi(ESP32 同形 API) */
#include <HTTPClient.h>      /* TuyaOpen HTTPClient 库 */
#include "http_client_interface.h"   /* 底层 HTTP（PCM 裸流长度自填，绕 strlen） */
#include "Audio.h"           /* 板载 codec 录音/放音（T5AI 专用，AudioRecorder 例程同源） */
#include "caremate_local.h"  // Copy caremate_local.example.h before compiling.
#include "t5ai_bitmaps.h"    /* 自动生成：中文按钮/状态条位图 */

/* ══════════ 现场校正区 ══════════ */
#define ARM_PORT      8013
/* TODO·授权三件套（涂鸦群免费领后填入；仅接涂鸦云需要，局域网链路不需要） */
#define TUYA_DEVICE_UUID    "uuidxxxxxxxxxxxxxxxx"
#define TUYA_DEVICE_AUTHKEY "keyxxxxxxxxxxxxxxxx"
/* 屏旋转：面板原生 320x480 竖，UI 按 480x320 横排。90=实测方向正确（状态条在顶） */
#define UI_ROTATION   LV_DISPLAY_ROTATION_90
/* 语音录制时长：4s(管线上电启动吃 ~1s,净得 3s) */
#define VOICE_MS      4000
#define VOICE_BUF     (VOICE_MS / 10 * 640)   /* Audio 库约定：micBufferSize = ms/10*640 */
/* ════════════════════════════════ */

/* 动作键表：位图 / HTTP 路径 / 位置(x,y) —— 480x320 布局（222x100 位图 + 底部通栏语音键） */
typedef struct { const lv_image_dsc_t *img; const char *ep; int x, y, w, h; } BtnCfg;
static BtnCfg BTNS[] = {
    { &BMP_BTN_MED,    "/handover_med",  12,  40, 222, 100 },  /* 递药盒（臂+手配合） */
    { &BMP_BTN_RETURN, "/return_med",    246,  40, 222, 100 },  /* 还药（臂+手配合）   */
    { &BMP_BTN_SOS,    "/press_sos",     12, 146, 222, 100 },  /* 按SOS（臂去按呼叫键）*/
    { &BMP_BTN_RESET,  "/reset",        246, 146, 222, 100 },  /* 归位                */
    { &BMP_BTN_VOICE,  "/voice_cmd",      12, 252, 456,  50 },  /* 点一下说话→录 3s */
};
#define N_BTN   (sizeof(BTNS) / sizeof(BTNS[0]))
#define VOICE_IDX   (N_BTN - 1)

static lv_obj_t *btn_objs[N_BTN];
static lv_obj_t *bar_img;                 /* 顶部状态条（位图） */
static const lv_image_dsc_t *bar_now = NULL;
static lv_obj_t *flash_obj;               /* 按下白框反馈（罩在按钮上） */
static volatile const char *pending_ep = NULL;   /* 待发端点：回调挂，loop 发 */
static bool net_ok = false;

/* ---------- 语音状态机（loop 驱动） ----------
 * idle → recording(点语音键) → 3s → posting(发 PCM) → idle
 */
enum VoiceState { V_IDLE, V_REC, V_POST };
static VoiceState v_state = V_IDLE;
static uint32_t v_t0 = 0;
static Audio audio;
static uint8_t *v_buf = NULL;             /* PCM 接收缓冲(一次性 malloc) */
static uint32_t v_len = 0;

/* ---------- HTTP（TuyaOpen HTTPClient 签名，照官方 BasicHttpClient 例程） ---------- */
static int http_post(const char *ep) {
    HTTPClient http;
    String url = String("http://") + ARM_HOST + ":" + ARM_PORT + ep;
    if (!http.begin(url)) return -1;
    http_client_header_t headers[] = {{.key = "Content-Type", .value = "application/json"}};
    http_client_response_t resp = {0};
    http_client_status_t st = http.POST(headers, 1, NULL, 0,
                                        (const uint8_t *)"{}", &resp);
    http.end(&resp);
    return (st == HTTP_CLIENT_SUCCESS) ? 200 : -1;
}

/* PCM 裸流 POST（/voice_cmd）：绕开 HTTPClient 封装——它内部 strlen(body) 量长度，
 * PCM 里全是随机的 0x00 会截断。直接用底层 http_client_request，body_length 自填 */
static int http_post_voice(const uint8_t *data, uint32_t len) {
    http_client_request_t req = {
        .host = ARM_HOST,
        .port = ARM_PORT,
        .path = "/voice_cmd",
        .cacert = NULL,
        .cacert_len = 0,
        .method = "POST",
        .headers = NULL,
        .headers_count = 0,
        .body = data,
        .body_length = len,
        .timeout_ms = 8000,
    };
    http_client_response_t resp = {0};
    http_client_status_t st = http_client_request(&req, &resp);
    int code = (st == HTTP_CLIENT_SUCCESS) ? 200 : -1;
    http_client_free(&resp);
    return code;
}

static int http_get_status(void) {
    HTTPClient http;
    String url = String("http://") + ARM_HOST + ":" + ARM_PORT + "/status";
    if (http.begin(url) != 0) return -1;
    http_client_response_t resp = {0};
    http_client_status_t st = http.GET(NULL, 0, NULL, 0, &resp);
    http.end(&resp);
    return (st == HTTP_CLIENT_SUCCESS) ? 200 : -1;
}

/* ---------- UI（全部 lv_vendor_disp_lock/unlock 包住，跑在 LVGL 线程） ---------- */
static void flash_hide_cb(lv_timer_t *t) {
    lv_obj_add_flag(flash_obj, LV_OBJ_FLAG_HIDDEN);
    lv_timer_del(t);
}

static void bar_set_locked(bool on, bool listen) {
    const lv_image_dsc_t *want = listen ? &BMP_BAR_LISTEN
                              : on     ? &BMP_BAR_ON
                                       : &BMP_BAR_OFF;
    if (bar_now != want && bar_img != NULL) {
        bar_now = want;
        lv_image_set_src(bar_img, want);
    }
}

static void btn_event_cb(lv_event_t *e) {
    intptr_t idx = (intptr_t)lv_event_get_user_data(e);
    /* 即时反馈：白框罩上。只动 UI 不碰网络——网络慢/挂了也不能冻界面
     * 注意：本回调跑在 LVGL 线程里（lv_task_handler 上下文），不能再拿
     * lv_vendor_disp_lock（非递归互斥，同线程 = 死锁），直接操作 UI 即可 */
    lv_obj_set_pos(flash_obj, BTNS[idx].x - 3, BTNS[idx].y - 3);
    lv_obj_set_size(flash_obj, BTNS[idx].w + 6, BTNS[idx].h + 6);
    lv_obj_clear_flag(flash_obj, LV_OBJ_FLAG_HIDDEN);
    if ((int)idx == (int)VOICE_IDX) {
        if (v_state != V_IDLE) {
            /* 已在录/发中：白框闪一下就收，忽略本次 */
            lv_timer_t *t = lv_timer_create(flash_hide_cb, 350, NULL);
            lv_timer_set_repeat_count(t, 1);
            return;
        }
        /* 语音键：开录 3 秒；白框常亮到发完（loop 里收）；状态条转橙"听音中" */
        audio.clearRecordedData();
        if (OPRT_OK != audio.startRecord()) {
            PR_ERR("startRecord FAIL\n");
            lv_timer_t *t = lv_timer_create(flash_hide_cb, 350, NULL);
            lv_timer_set_repeat_count(t, 1);
            return;
        }
        bar_set_locked(net_ok, true);
        v_state = V_REC;
        v_t0 = millis();
    } else {
        lv_timer_t *t = lv_timer_create(flash_hide_cb, 350, NULL);
        lv_timer_set_repeat_count(t, 1);
        pending_ep = BTNS[idx].ep;      /* 发送交给 loop()（异步） */
    }
}

static void ui_build(void) {
    lv_vendor_disp_lock();

    lv_obj_set_style_bg_color(lv_screen_active(), lv_color_hex(0x10141a), LV_PART_MAIN);

    /* 顶部状态条 480x34（位图，红=未连接/绿=已连接/橙=听音中） */
    bar_img = lv_image_create(lv_screen_active());
    lv_image_set_src(bar_img, &BMP_BAR_OFF);
    lv_obj_set_pos(bar_img, 0, 0);
    bar_now = &BMP_BAR_OFF;

    /* 动作大按钮 = 中文位图（点图本身收事件） */
    for (intptr_t i = 0; i < (intptr_t)N_BTN; i++) {
        lv_obj_t *b = lv_image_create(lv_screen_active());
        btn_objs[i] = b;
        lv_image_set_src(b, BTNS[i].img);
        lv_obj_set_pos(b, BTNS[i].x, BTNS[i].y);
        lv_obj_set_size(b, BTNS[i].w, BTNS[i].h);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(b, btn_event_cb, LV_EVENT_CLICKED, (void *)i);
    }

    /* 按下反馈白框（罩层，默认藏；不吃点击） */
    flash_obj = lv_obj_create(lv_screen_active());
    lv_obj_set_size(flash_obj, 228, 106);
    lv_obj_set_style_bg_opa(flash_obj, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_color(flash_obj, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_border_width(flash_obj, 3, LV_PART_MAIN);
    lv_obj_set_style_radius(flash_obj, 15, LV_PART_MAIN);
    lv_obj_remove_flag(flash_obj, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(flash_obj, LV_OBJ_FLAG_HIDDEN);

    lv_vendor_disp_unlock();
    lv_vendor_start(5, 1024 * 8);
}

/* ---------- 主流程 ---------- */
void setup(void)
{
    Serial.begin(115200);
    Log.begin(1024);

    if (OPRT_OK != board_register_hardware()) {
        PR_ERR("board_register_hardware error\n");
        while (1) {}
    }

    /* 网络：连 Mocen_Bear（WiFi API：begin/status 与 ESP32 同形，状态码是 WSS_*） */
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    for (int i = 0; i < 200 && WiFi.status() != WSS_GOT_IP; i++) {
        delay(100);   /* 最多等 20 秒 */
    }
    PR_DEBUG("wifi %s ip=%s\n",
             WiFi.status() == WSS_GOT_IP ? "OK" : "FAIL",
             WiFi.localIP().toString().c_str());

    /* LVGL 厂商层初始化(官方 LVGLdemo 同序;设备名与板层注册的显示设备一致) */
    static char display_name[] = "display";
    lv_vendor_init(display_name);
    /* 面板原生竖屏(320x480),UI 按横屏(480x320)布局——整屏旋转 */
    lv_display_set_rotation(lv_display_get_default(), UI_ROTATION);

    ui_build();

    /* 音频：16k/16bit/单声道（库内固定），mic 缓冲 3 秒 */
    AudioConfig cfg;
    cfg.micBufferSize = VOICE_BUF;
    cfg.volume = 70;
    if (OPRT_OK != audio.begin(&cfg)) {
        PR_ERR("audio begin FAIL\n");
    }
    v_buf = (uint8_t *)tal_psram_malloc(VOICE_BUF);   /* 192k 走 PSRAM，堆装不下 */
    PR_DEBUG("voice ready buf=%p len<=%u\n", v_buf, (unsigned)VOICE_BUF);
}

void loop()
{
    /* ── 语音状态机 ── */
    if (v_state == V_REC) {
        if (audio.isRecording() && millis() - v_t0 >= VOICE_MS) {
            audio.stopRecord();
        }
        if (!audio.isRecording()) {               /* 录满 3s（或提前结束） */
            uint32_t used = audio.getRecordedDataLen();
            v_len = v_buf ? audio.readRecordedData(v_buf, VOICE_BUF) : 0;
            PR_DEBUG("voice done: buf=%p used=%u read=%u\n",
                     v_buf, (unsigned)used, (unsigned)v_len);
            lv_obj_add_flag(flash_obj, LV_OBJ_FLAG_HIDDEN);
            if (v_len > 0) {
                v_state = V_POST;                  /* 发送也挂 loop（不冻 UI） */
            } else {
                v_state = V_IDLE;                  /* 没录到：回常态 */
            }
        }
    }
    if (v_state == V_POST) {
        uint32_t t0 = millis();
        int code = http_post_voice(v_buf, v_len);
        PR_DEBUG("voice POST -> %d (%u ms)\n", code, (unsigned)(millis() - t0));
        v_state = V_IDLE;
        /* 状态条回真值（下一次轮询也会校正） */
        lv_vendor_disp_lock();
        bar_set_locked(net_ok, false);
        lv_vendor_disp_unlock();
    }

    /* ── 异步动作键发送 ── */
    const char *ep = (const char *)pending_ep;
    if (ep != NULL) {
        pending_ep = NULL;
        int code = http_post(ep);
        PR_DEBUG("http POST %s -> %d\n", ep, code);
        if (code == 200) {
            net_ok = true;
            lv_vendor_disp_lock();
            bar_set_locked(true, false);
            lv_vendor_disp_unlock();
        }
    }

    /* ── 2s 在线轮询 ── */
    static uint32_t last_poll = 0;
    if (millis() - last_poll > 2000 && v_state == V_IDLE) {
        last_poll = millis();
        net_ok = (http_get_status() == 200);
        lv_vendor_disp_lock();
        bar_set_locked(net_ok, false);
        lv_vendor_disp_unlock();
    }
    delay(10);
}