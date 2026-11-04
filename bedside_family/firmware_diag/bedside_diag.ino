/* CareMate 床头盒显示诊断 v5 —— ESP32_Display_Panel 维护者跑通配置（剥掉 LVGL）
 *
 * 跑通配置来源：ESP32_Display_Panel issue #129 中 Lzw655 的 Porting_test.zip
 *   SPI mode=0, 40MHz, 240x320, CS 经 PCA9557@0x19（I2C SDA=1 SCL=2）, BL=42 低电平, invert=1
 *
 * 循环显示 4 色全屏块（每色 10 秒）：红/绿/蓝/白
 * 哪个颜色能亮 = 判定显示链路是否打通。
 */

#include <Arduino.h>
#include <ESP_Panel_Library.h>

#define LCD_BL 42

static ESP_Panel *panel = NULL;
static uint8_t buf[240 * 320 * 2];
static const int W = 240, H = 320;

static void fillBuf(uint8_t hi, uint8_t lo) {
  for (int i = 0; i < W * H; i++) { buf[i * 2] = hi; buf[i * 2 + 1] = lo; }
}

static void show(const char* tag, uint8_t hi, uint8_t lo, unsigned long ms) {
  fillBuf(hi, lo);
  bool ok = panel->getLCD()->drawBitmap(0, 0, W, H, buf);
  Serial.printf("[diag] %s drawBitmap ret=%d\n", tag, ok);
  delay(ms);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[diag] v5 ESP32_Display_Panel route");

  panel = new ESP_Panel();
  bool i1 = panel->init();
  bool b1 = panel->begin();
  Serial.printf("[diag] panel init=%d begin=%d\n", i1, b1);

  /* 背光：PWM_LEDC（配置 42 低有效）——begin 后开 */
  panel->getBacklight()->on();

  fillBuf(0xF8, 0x00);
  panel->getLCD()->drawBitmap(0, 0, W, H, buf);
  Serial.println("[diag] red shown");
}

void loop() {
  Serial.println("[diag] RED");
  show("RED", 0xF8, 0x00, 8000);
  Serial.println("[diag] GREEN");
  show("GREEN", 0x07, 0xE0, 8000);
  Serial.println("[diag] BLUE");
  show("BLUE", 0x00, 0x1F, 8000);
  Serial.println("[diag] WHITE");
  show("WHITE", 0xFF, 0xFF, 8000);
}