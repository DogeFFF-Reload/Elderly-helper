package com.mocen.caremate.family;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

/** 守护服务:app 关着也能拉警报,10 秒一轮;发现新跌倒警报 → 系统高优通知(响铃+震动) */
public class GuardService extends Service {

    private static final String CH_ALARM = "caremate_alarm";
    private volatile boolean running = true;
    private volatile boolean threadUp = false;   // 防止重复打开 app 起多个轮询线程
    private Handler h;

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public int onStartCommand(Intent i, int f, int id) {
        h = new Handler(Looper.getMainLooper());
        start();
        return START_STICKY;
    }

    private void start() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        Notification ch = mkChannel(nm, "caremate_alive", "守护在线");
        try {
            startForeground(1, ch);
        } catch (Exception e) { return; }
        if (threadUp) return;                       // 已在轮询,不重复起线程
        threadUp = true;

        new Thread(() -> {
            SharedPreferences pf = getSharedPreferences("alarm", 0);
            int tick = 0;
            while (running) {
                String base = pickBase();
                if (base != null) {
                    try {
                        String j = httpGet(base + "/alarms");
                        JSONArray arr = new JSONObject(j).getJSONArray("alarms");
                        String newest = null;
                        for (int k = 0; k < arr.length(); k++) {
                            org.json.JSONObject a = arr.getJSONObject(k);
                            if (!a.optBoolean("acked")) { newest = a.optString("id"); break; }
                        }
                        if (newest != null && !newest.equals(pf.getString("sent", ""))) {
                            String who = "";
                            for (int k = 0; k < arr.length(); k++) {
                                org.json.JSONObject a = arr.getJSONObject(k);
                                if (a.optString("id").equals(newest)) {
                                    who = a.optString("time") + (a.has("src") && !a.optString("src").isEmpty()
                                            ? " · 来源 " + a.optString("src") : "");
                                    break;
                                }
                            }
                            pf.edit().putString("sent", newest).apply();
                            notifyAlarm(nm, newest, who);
                        }
                    } catch (Exception e) { /* 网络/解析失败:静默下轮再试 */ }
                }
                try { Thread.sleep(tick++ == 0 ? 4000 : 10000); } catch (Exception e) { return; }
            }
        }, "guard-poll").start();
    }

    private String pickBase() {
        if (!Cfg.CLOUD_BASE.isEmpty() && ok(Cfg.CLOUD_BASE + "/alarms")) return Cfg.CLOUD_BASE;
        if (ok(Cfg.LAN_BASE + "/alarms")) return Cfg.LAN_BASE;
        return null;
    }

    private boolean ok(String u) {
        try {
            java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(u).openConnection();
            c.setConnectTimeout(2500); c.setReadTimeout(2500);
            int code = c.getResponseCode(); c.disconnect();
            return code == 200;
        } catch (Exception e) { return false; }
    }

    private String httpGet(String u) throws Exception {
        java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(u).openConnection();
        c.setConnectTimeout(3500); c.setReadTimeout(5000);
        java.io.InputStream is = c.getInputStream();
        java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
        byte[] bb = new byte[8192]; int n;
        while ((n = is.read(bb)) > 0) b.write(bb, 0, n);
        is.close(); c.disconnect();
        return new String(b.toByteArray(), "UTF-8");
    }

    private void notifyAlarm(NotificationManager nm, String aid, String who) {
        PendingIntent pi = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class)
                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(this, CH_ALARM)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("⚠ 跌倒警报")
                .setContentText("检测到老人疑似跌倒!" + who)
                .setContentIntent(pi)
                .setAutoCancel(true);
        nm.notify(1001, b.build());
    }

    private Notification mkChannel(NotificationManager nm, String chId, String chName) {
        Notification n;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel low = new NotificationChannel(
                    "caremate_alive", "守护在线", NotificationManager.IMPORTANCE_MIN);
            nm.createNotificationChannel(low);
            NotificationChannel hl = new NotificationChannel(CH_ALARM, "跌倒警报",
                    NotificationManager.IMPORTANCE_HIGH);
            hl.enableVibration(true);
            hl.setSound(android.provider.Settings.System.DEFAULT_ALARM_ALERT_URI,
                    new android.media.AudioAttributes.Builder()
                            .setUsage(android.media.AudioAttributes.USAGE_ALARM).build());
            try { hl.setBypassDnd(true); } catch (Exception e) {}
            nm.createNotificationChannel(hl);
            n = new Notification.Builder(this, "caremate_alive")
                    .setSmallIcon(android.R.drawable.stat_notify_chat)
                    .setContentTitle("家人留言 · 守护中")
                    .setContentText("后台监听新留言与跌倒警报").build();
        } else {
            n = new Notification.Builder(this)
                    .setSmallIcon(android.R.drawable.stat_notify_chat)
                    .setContentTitle("家人留言 · 守护中").build();
        }
        return n;
    }

    @Override public void onDestroy() {
        running = false;
        threadUp = false;
        super.onDestroy();
    }

    @SuppressWarnings("deprecation")
    private String prefGet(SharedPreferences pf, String k, String d) { return pf.getString(k, d); }
}