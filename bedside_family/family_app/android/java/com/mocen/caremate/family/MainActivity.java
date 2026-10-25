package com.mocen.caremate.family;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * CareMate 家人留言 —— 原生安卓版
 * 听 TA 说:老人的语音留言列表播放(msg_*)
 * 我来说:按住说话→试听→发给 TA(fam_*)
 * 数据源:自动探测 公网中转(Cfg.CLOUD_BASE)→ 记住的地址 → 床头直连(Cfg.LAN_BASE)→ 扫网段
 *
 * v1.8 线程规则:主线程只动界面;所有网络请求走 NET 单线程(不并发、不堆积),播放只走 PLAY_EXEC。
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 7;
    private static final int PURPLE = 0xFF623FD2, PURPLE_L = 0xFFEDE7FB, BG = 0xFFF6F4FB,
            TXT = 0xFF2A2140, GREEN = 0xFF3BA55D, RED = 0xFFE5484D, GRAY = 0xFF9A94AC;
    private static final int ALARM_NOTIFY_ID = 1001;   // 与 GuardService 的警报通知同号
    private static final long POLL_MS = 3000;

    /** 前台可见时 GuardService 不再弹系统警报通知(避免 app 内警铃 + 通知铃声两声叠响)。 */
    static volatile boolean visible = false;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private static final ExecutorService NET = Executors.newSingleThreadExecutor(r -> new Thread(r, "net"));
    private final AtomicBoolean pollQueued = new AtomicBoolean(false);
    private volatile String base = Cfg.LAN_BASE;  // 当前生效的数据源(NET 线程写,主线程读)
    private LinearLayout pageListen, pageTalk;
    private TextView connState, tabL, tabR, stateLabel, listTip, timerLabel;
    private LinearLayout cardBox;                // 留言卡片容器(在 ScrollView 里)
    private LinearLayout talkPanel;
    private Button sendBtn;
    private final List<Msg> msgs = new ArrayList<>();
    private final ArrayList<TextView> playBtns = new ArrayList<>();   // v1.8:每张卡的播放按钮(按下反馈用)

    /* ============ 预下载(v1.9:列表一刷新就后台缓存最新12条,点播放秒出声) ============
     * 板子拉一条 wav 实测 0.3~5s,点播时才下=有时等好几秒。改为后台预下载到缓存:
     * 点播时命中缓存→本地读,毫秒级起播;未命中才走网络(12s 超时,按钮有进度提示)。 */
    private static final java.util.concurrent.ExecutorService PRE_FETCH =
            java.util.concurrent.Executors.newSingleThreadExecutor(r -> new Thread(r, "prefetch"));
    private final java.util.HashSet<String> prefetched = new java.util.HashSet<>();

    private java.io.File cacheOf(String id) {
        try { return new java.io.File(getCacheDir(), id + ".wav"); } catch (Exception e) { return null; }
    }

    private void prefetchMsgs() {
        if (msgs.isEmpty()) return;
        final String b = base;
        PRE_FETCH.execute(() -> {
            try {
                for (int i = msgs.size() - 1; i >= 0 && i >= msgs.size() - 12; i--) {
                    Msg m = msgs.get(i);
                    if (m == null || prefetched.contains(m.id)) continue;
                    java.io.File f = cacheOf(m.id);
                    if (f != null && f.exists() && f.length() > 1000) { prefetched.add(m.id); continue; }
                    try {
                        byte[] w = httpGetbin(b + "/messages/" + m.id + ".wav", 20000);
                        java.io.File t = new java.io.File(getCacheDir(), m.id + ".tmp");
                        java.io.FileOutputStream fo = new java.io.FileOutputStream(t);
                        fo.write(w); fo.close();
                        if (t.length() > 1000 && !t.renameTo(f)) t.delete();
                        prefetched.add(m.id);
                    } catch (Exception e) { break; }   // 链路抖:这轮先停,下次刷新再续
                }
            } catch (Exception e) {}
        });
    }

    private String listSig = "";                 // 列表内容签名:没变就不重建,滚动位置不跳

    // 录音
    private AudioRecord rec;
    private Thread recThread;
    private volatile boolean recing = false;
    private boolean holding = false;             // 手指是否还按在录音钮上
    private final ByteArrayOutputStream pcmBuf = new ByteArrayOutputStream();
    private long recStart;
    private File recFile;                        // 录完的 wav
    private MediaPlayer player;                  // 播放列表/试听共用(只在 PLAY_EXEC 线程动)
    private int playingPos = -1;                 // 列表播放中的位置(只在 PLAY_EXEC 线程动)

    // 跌倒警报
    private final List<Alarm> alarms = new ArrayList<>();
    private LinearLayout alarmBar;               // 顶部红色警报条(全局)
    private TextView alarmText;
    private MediaPlayer alarmSnd;
    private Vibrator vib;
    private String ringingId = null;             // app 内正在响的警报;null = 没在响
    private final Set<String> ackedHere = new HashSet<>();   // 本机已点「知道了」的(不再重响)
    private LinearLayout permBar;                // 琥珀色权限提醒条(通知权限/后台常驻)
    private TextView permText;
    private boolean notifAsked = false;          // 本次进程只自动弹一次通知权限
    private TextView guardLine;                  // 守护自检行:轮询年龄+权限状态,熄屏测试后一瞄即知服务死活

    static class Msg { String id, time; double dur; boolean own; }
    static class Alarm { String id, time, src; boolean acked; }

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        buildUi();
        vib = (Vibrator) getSystemService(VIBRATOR_SERVICE);
        try { startForegroundService(new Intent(this, GuardService.class)); } catch (Exception e) {}
        probeBase();
        refresh();
    }

    @Override protected void onResume() {
        super.onResume();
        visible = true;
        refreshPermBanner();
        ui.removeCallbacks(alarmPoll);
        ui.post(alarmPoll);                       // 立刻拉一次,之后 3 秒一轮(只在前台跑)
    }

    @Override protected void onPause() {
        super.onPause();
        visible = false;                          // 退到后台:轮询交给 GuardService
        ui.removeCallbacks(alarmPoll);
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        visible = false;
        ui.removeCallbacksAndMessages(null);      // 停掉轮询 / 录音计时等所有排队任务
        stopRec(true);
        stopAlarmNoise();
        PLAY_EXEC.execute(this::releasePlayer);   // 播放器只允许在 player 线程动:串行排队释放
        if (recFile != null && recFile.exists()) recFile.delete();
    }

    /* ================= UI(纯代码,无 XML 布局) ================= */

    private int dp(float v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private GradientDrawable round(int fill, int r) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill); g.setCornerRadius(dp(r));
        return g;
    }

    private TextView text(String s, int size, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s); t.setTextSize(size); t.setTextColor(color);
        t.setTypeface(Typeface.DEFAULT, bold ? Typeface.BOLD : Typeface.NORMAL);
        return t;
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);

        // 顶部红色警报条(默认隐藏;有新跌倒警报时置顶弹出)
        alarmBar = new LinearLayout(this);
        alarmBar.setOrientation(LinearLayout.HORIZONTAL);
        alarmBar.setGravity(Gravity.CENTER_VERTICAL);
        alarmBar.setBackgroundColor(RED);
        alarmBar.setPadding(dp(14), dp(6), dp(10), dp(6));
        alarmBar.setVisibility(View.GONE);
        alarmBar.setClickable(true);
        alarmBar.setOnClickListener(v -> {});          // 拦截点击,防穿到列表
        alarmText = text("⚠ 跌倒警报", 15, 0xFFFFFFFF, true);
        alarmText.setPadding(0, dp(6), 0, dp(6));
        alarmText.setLayoutParams(new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        Button okb = btn("知道了", 0xFFFFFFFF, RED, v -> ackAlarm());
        alarmBar.addView(alarmText);
        alarmBar.addView(okb);
        root.addView(alarmBar);

        // 权限提醒条(琥珀色,可点):通知权限/后台常驻 —— 熄屏收警报的生死线,缺哪样亮哪条
        permBar = new LinearLayout(this);
        permBar.setOrientation(LinearLayout.HORIZONTAL);
        permBar.setGravity(Gravity.CENTER_VERTICAL);
        permBar.setBackgroundColor(0xFFFFB300);
        permBar.setPadding(dp(14), dp(6), dp(10), dp(6));
        permBar.setVisibility(View.GONE);
        permText = text("⚠ 提醒权限未开全——熄屏/退后台将收不到警报", 13, 0xFF3E2C00, true);
        permText.setPadding(0, dp(4), 0, dp(4));
        permText.setLayoutParams(new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        Button pb = btn("去开启", 0xFF3E2C00, 0xFFFFB300, v -> openPermSettings());
        permBar.addView(permText);
        permBar.addView(pb);
        root.addView(permBar);

        // 守护自检行:熄屏测完回来一瞄,"轮询 3 秒前 ✓"=服务活着只是通知没弹;"停了?"=被杀/冻结/断网
        guardLine = text("守护: 自检中…", 11, GRAY, false);
        guardLine.setPadding(dp(4), dp(3), dp(4), 0);
        guardLine.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(guardLine);

        // 顶部
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.VERTICAL);
        header.setGravity(Gravity.CENTER);
        header.setBackgroundColor(PURPLE);
        header.setPadding(dp(16), dp(28), dp(16), dp(10));
        TextView title = text("CareMate 家人留言", 19, 0xFFFFFFFF, true);
        title.setGravity(Gravity.CENTER);
        connState = text("连接中…", 12, 0xFFD8D2F0, false);   // v1.7 少了 alpha 字节,整行透明看不见
        connState.setGravity(Gravity.CENTER);
        connState.setPadding(0, dp(3), 0, 0);
        header.addView(title); header.addView(connState);
        root.addView(header);

        // 标签
        LinearLayout tabs = new LinearLayout(this);
        tabs.setBackgroundColor(PURPLE);
        tabs.setPadding(dp(12), 0, dp(12), dp(10));
        tabs.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams tl = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        tabL = text("听 TA 说", 15, PURPLE, true);
        tabL.setGravity(Gravity.CENTER); tabL.setPadding(0, dp(9), 0, dp(9));
        tabL.setBackground(round(BG, 12)); tabL.setLayoutParams(tl);
        tabR = text("我来说", 15, 0xFFFFFFFF, true);
        tabR.setGravity(Gravity.CENTER); tabR.setPadding(0, dp(9), 0, dp(9));
        tabR.setBackground(round(0x29FFFFFF, 12)); tabR.setLayoutParams(tl);
        LinearLayout spacer = new LinearLayout(this);
        spacer.setLayoutParams(new LinearLayout.LayoutParams(dp(8), 1));
        tabs.addView(tabL); tabs.addView(spacer); tabs.addView(tabR);
        tabL.setOnClickListener(v -> swTab(0));
        tabR.setOnClickListener(v -> swTab(1));
        root.addView(tabs);

        // 列表页
        pageListen = new LinearLayout(this);
        pageListen.setOrientation(LinearLayout.VERTICAL);
        listTip = text("", 12, GRAY, false);
        listTip.setGravity(Gravity.CENTER); listTip.setPadding(dp(12), dp(10), dp(12), dp(2));
        pageListen.addView(listTip, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        android.widget.ScrollView sv = new android.widget.ScrollView(this);
        sv.setVerticalScrollBarEnabled(false);
        cardBox = new LinearLayout(this);
        cardBox.setOrientation(LinearLayout.VERTICAL);
        cardBox.setPadding(dp(10), dp(4), dp(10), dp(4));
        sv.addView(cardBox, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        pageListen.addView(sv, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        Button re = new Button(this);
        re.setText("↻ 刷新"); re.setTextSize(14); re.setTextColor(PURPLE);
        re.setAllCaps(false);
        re.setBackground(round(0xFFFFFFFF, 22));
        re.setOnClickListener(v -> { listSig = ""; probeBase(); refresh(); });
        LinearLayout.LayoutParams rl = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rl.gravity = Gravity.CENTER_HORIZONTAL; rl.setMargins(0, dp(8), 0, dp(16));
        pageListen.addView(re, rl);
        root.addView(pageListen, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

        // 录音页
        pageTalk = new LinearLayout(this);
        pageTalk.setOrientation(LinearLayout.VERTICAL);
        pageTalk.setGravity(Gravity.CENTER);
        pageTalk.setVisibility(View.GONE);
        timerLabel = text("0.0s", 30, PURPLE, true);
        timerLabel.setGravity(Gravity.CENTER);           // 整行宽的 TextView:文字要自己居中(v1.7 贴左边)
        pageTalk.addView(timerLabel);

        TextView rbtn = text("🎙  按住说话", 18, 0xFFFFFFFF, true);
        rbtn.setGravity(Gravity.CENTER);
        GradientDrawable rb = round(PURPLE, 66);
        rb.setStroke(dp(5), PURPLE_L);
        rbtn.setBackground(rb);
        rbtn.setPadding(dp(10), dp(30), dp(10), dp(30));
        LinearLayout.LayoutParams rl2 = new LinearLayout.LayoutParams(dp(190), dp(190));
        rl2.topMargin = dp(28); rl2.bottomMargin = dp(24);
        rbtn.setOnTouchListener((v, e) -> {
            switch (e.getActionMasked()) {
                case android.view.MotionEvent.ACTION_DOWN:
                    holding = true; startRec(); return true;
                case android.view.MotionEvent.ACTION_UP:
                case android.view.MotionEvent.ACTION_CANCEL:
                    holding = false; stopRec(false); return true;
            }
            return true;
        });
        pageTalk.addView(rbtn, rl2);

        stateLabel = text("按住圆钮开始,松手说完", 14, GRAY, false);
        stateLabel.setGravity(Gravity.CENTER);
        stateLabel.setPadding(dp(16), 0, dp(16), 0);
        pageTalk.addView(stateLabel);

        talkPanel = new LinearLayout(this);
        talkPanel.setOrientation(LinearLayout.HORIZONTAL);
        talkPanel.setGravity(Gravity.CENTER);
        talkPanel.setVisibility(View.GONE);
        talkPanel.addView(btn("重录", 0xFFEEEEEE, TXT, v -> { clearRec(); stateLabel.setText("按住圆钮重新说"); }));
        talkPanel.addView(btn("▶ 试听", 0xFFFFFFFF, PURPLE, v -> playFileOrToast()));
        sendBtn = btn("发给 TA", GREEN, 0xFFFFFFFF, v -> sendRec());
        talkPanel.addView(sendBtn);
        LinearLayout.LayoutParams pl = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        pl.topMargin = dp(20);
        pageTalk.addView(talkPanel, pl);
        root.addView(pageTalk, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

        setContentView(root);
    }

    private Button btn(String s, int bg, int fg, View.OnClickListener c) {
        Button b = new Button(this);
        b.setText(s); b.setTextSize(15); b.setTextColor(fg);
        b.setBackground(round(bg, 24));
        b.setAllCaps(false);
        LinearLayout.LayoutParams l = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        l.setMargins(dp(6), 0, dp(6), 0);
        b.setLayoutParams(l);
        b.setOnClickListener(c);
        return b;
    }

    private void swTab(int i) {
        pageListen.setVisibility(i == 0 ? View.VISIBLE : View.GONE);
        pageTalk.setVisibility(i == 1 ? View.VISIBLE : View.GONE);
        tabL.setBackground(round(i == 0 ? BG : 0x29FFFFFF, 12));
        tabL.setTextColor(i == 0 ? PURPLE : 0xFFFFFFFF);
        tabR.setBackground(round(i == 1 ? BG : 0x29FFFFFF, 12));
        tabR.setTextColor(i == 1 ? PURPLE : 0xFFFFFFFF);
        if (i == 0) refresh();
    }

    /* ================= 数据源探测 ================= */

    private void probeBase() {
        NET.execute(() -> {
            String pick = null;
            if (!Cfg.CLOUD_BASE.isEmpty() && ok200(Cfg.CLOUD_BASE + "/messages")) pick = Cfg.CLOUD_BASE;
            String saved = getSharedPreferences("alarm", 0).getString("base", "");
            if (pick == null && !saved.isEmpty() && ok200(saved + "/messages")) pick = saved;   // 先试上次找到的
            if (pick == null && ok200(Cfg.LAN_BASE + "/messages")) pick = Cfg.LAN_BASE;
            if (pick == null) pick = discoverBase();     // v1.5 链级自愈:板子换网段/换 IP 也能找回来
            final String f = pick;
            if (f != null) base = f;
            ui.post(() -> connState.setText(f != null ? "已连接老人床头 · Mocen"
                    : "没找到床头设备(和床头连同一个网络再打开)"));
        });
    }

    /** v1.5 网络自愈:静态地址连不上时,扫本机每个网段(/24)的 8013 端口,
     *  命中 /status 即为床头;找到后存 prefs,本轮直接可用(热点换网段不再断链)。 */
    static volatile String remembered = null;            // 进程级缓存,避免每轮全扫

    private String discoverBase() {
        String saved = getSharedPreferences("alarm", 0).getString("base", "");
        if (!saved.isEmpty() && ok200(saved + "/messages")) { remembered = saved; return saved; }
        String hit = subnetScan();
        if (hit != null) {
            remembered = hit;
            getSharedPreferences("alarm", 0).edit().putString("base", hit).apply();
        }
        return hit;
    }

    /** 枚举本机所有 IPv4(去环回/本地链路)→ 并行扫各自 /24 的 8013:/status。 */
    static volatile long lastScan = 0;                   // 扫描冷却:别每轮轮询都全扫

    static String subnetScan() {
        synchronized (MainActivity.class) {              // App 与守护服务可能同时触发:只放一个进去
            long now = System.currentTimeMillis();
            if (now - lastScan < 20000) return null;     // 20s 冷却
            lastScan = now;
        }
        java.util.Set<String> nets = new java.util.LinkedHashSet<>();
        try {
            java.util.Enumeration<java.net.NetworkInterface> ifs =
                    java.net.NetworkInterface.getNetworkInterfaces();
            while (ifs != null && ifs.hasMoreElements()) {
                java.net.NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                java.util.Enumeration<java.net.InetAddress> ads = ni.getInetAddresses();
                while (ads.hasMoreElements()) {
                    java.net.InetAddress a = ads.nextElement();
                    if (!a.isLoopbackAddress() && a instanceof java.net.Inet4Address
                            && !a.isLinkLocalAddress()) {
                        byte[] ip = a.getAddress();
                        nets.add((ip[0] & 0xFF) + "." + (ip[1] & 0xFF) + "." + (ip[2] & 0xFF) + ".");
                    }
                }
            }
        } catch (Exception e) { /* 没有任何网段:扫不了 */ }
        if (nets.isEmpty()) return null;
        List<String> bases = new ArrayList<>();
        for (String n : nets)
            for (int i = 1; i <= 254; i++) bases.add("http://" + n + i + ":8013");
        // 命中即返回(短路):并行 24 线程,单目标 350ms 超时,/24 最坏 ~4s
        java.util.concurrent.atomic.AtomicReference<String> found =
                new java.util.concurrent.atomic.AtomicReference<>(null);
        ExecutorService pool = Executors.newFixedThreadPool(24);
        List<java.util.concurrent.Future<?>> fs = new ArrayList<>();
        for (final String b : bases) {
            fs.add(pool.submit(() -> {
                if (found.get() != null) return;
                try {
                    HttpURLConnection c = open(b + "/status");
                    c.setConnectTimeout(350); c.setReadTimeout(350);
                    InputStream is = c.getInputStream();
                    byte[] bb = new byte[64];
                    int n = is.read(bb); is.close(); c.disconnect();
                    if (n > 0 && new String(bb, 0, n, "UTF-8").contains("backend")) {
                        found.compareAndSet(null, b);
                    }
                } catch (Exception e) { /* 不在线,继续 */ }
            }));
        }
        for (java.util.concurrent.Future<?> f : fs) {
            try { f.get(); } catch (Exception e) {}
        }
        pool.shutdownNow();
        return found.get();
    }

    /** 连床头一律直连:局域网地址绕过系统 HTTP 代理(手机开了翻墙 / 代理软件时,v1.7 的请求全被送进代理,
     *  代理够不到家里的局域网 → 整台手机连不上床头)。公网中转(CLOUD_BASE)仍按系统设置走。 */
    static HttpURLConnection open(String u) throws java.io.IOException {
        URL url = new URL(u);
        return (HttpURLConnection) (isLan(url.getHost())
                ? url.openConnection(java.net.Proxy.NO_PROXY) : url.openConnection());
    }

    static boolean isLan(String h) {
        if (h == null) return false;
        if (h.startsWith("10.") || h.startsWith("192.168.") || h.startsWith("169.254.")
                || h.startsWith("127.") || h.equals("localhost")) return true;
        if (h.startsWith("172.")) {
            try { int b = Integer.parseInt(h.split("\\.")[1]); return b >= 16 && b <= 31; }
            catch (Exception e) { return false; }
        }
        return false;
    }

    private boolean ok200(String u) {
        try {
            HttpURLConnection c = open(u);
            c.setConnectTimeout(Cfg.PROBE_TIMEOUT); c.setReadTimeout(Cfg.PROBE_TIMEOUT);
            int code = c.getResponseCode();
            c.disconnect();
            return code == 200;
        } catch (Exception e) { return false; }
    }

    /* ================= 列表 ================= */

    /** 刷新列表:NET 线程拉 JSON → 主线程重建卡片(内容没变就不重建) */
    private void refresh() {
        NET.execute(() -> {
            try {
                String j = httpGet(base + "/messages");
                JSONArray arr = new JSONObject(j).getJSONArray("messages");
                List<Msg> nm = new ArrayList<>();
                StringBuilder sig = new StringBuilder();
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    Msg m = new Msg();
                    m.id = o.getString("id"); m.time = o.optString("time");
                    m.dur = o.optDouble("duration", 0);
                    m.own = m.id.startsWith("fam_");
                    nm.add(m);
                    sig.append(m.id).append('|');
                }
                final String s = sig.toString();
                ui.post(() -> {
                    connState.setText("已连接老人床头 · Mocen");
                    if (s.equals(listSig)) return;        // 没有新留言:不动列表,正在看的位置不跳
                    listSig = s;
                    msgs.clear(); msgs.addAll(nm); fillList(); prefetchMsgs();   // v1.9:后台预缓存最新 12 条
                });
            } catch (Exception e) {
                ui.post(() -> {
                    connState.setText("没找到床头设备(和床头连同一个网络再打开)");
                    listTip.setText("连不上床头:手机要和床头在同一个网络,点刷新重试");
                    if (!listSig.isEmpty()) { listSig = ""; msgs.clear(); fillList(); }
                });
            }
        });
    }

    private void fillList() {
        cardBox.removeAllViews();
        playBtns.clear();
        // v1.6:已确认警报记录卡撤下(用户反馈:置顶的历史红卡差点让人以为留言收不到);
        // 真警报的表达只有两处:顶部全屏红条+警铃(前台) / 系统高优通知(后台),历史不再列表展示
        int newCnt = 0;
        String seen = getPreferences(0).getString("seen", "");
        String newest = newestElderId();
        if (newest != null && !newest.equals(seen)) {
            if (seen.isEmpty()) { for (Msg m : msgs) if (!m.own) newCnt++; }
            else { for (Msg m : msgs) { if (!m.own) { if (m.id.equals(seen)) break; newCnt++; } } }
            getPreferences(0).edit().putString("seen", newest).apply();
        }
        if (newest == null)
            listTip.setText("");
        else if (newCnt > 0)
            listTip.setText("有 " + newCnt + " 条新留言,点一条听原声");
        else
            listTip.setText("点一条听 TA 的原声");
        if (msgs.isEmpty()) {
            TextView e2 = text("还没有 TA 的留言\n老人在床头屏按「留言」说给你听", 14, GRAY, false);
            e2.setGravity(Gravity.CENTER); e2.setPadding(dp(12), dp(70), dp(12), dp(12));
            cardBox.addView(e2);
            return;
        }
        boolean shownMine = false;
        for (int i = 0; i < msgs.size(); i++) {
            Msg m = msgs.get(i);
            if (m.own && !shownMine) {
                cardBox.addView(sepTitle("— 我发过去的回话 —"));
                shownMine = true;
            }
            cardBox.addView(msgCard(m, i));
        }
    }

    private TextView sepTitle(String s) {
        TextView t = text(s, 12, GRAY, false);
        t.setGravity(Gravity.CENTER);
        t.setPadding(0, dp(14), 0, dp(6));
        return t;
    }

    private String newestElderId() { for (Msg m : msgs) if (!m.own) return m.id; return null; }

    private View msgCard(Msg m, int pos) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(round(0xFFFFFFFF, 16));
        card.setPadding(dp(15), dp(13), dp(15), dp(13));
        LinearLayout.LayoutParams cl = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        cl.bottomMargin = dp(10);
        card.setLayoutParams(cl);

        LinearLayout head = new LinearLayout(this);
        TextView dot = text("●", 12, m.own ? GREEN : PURPLE, true);
        TextView who = text(m.own ? "  我说的回话" : "  老人说 · 原声语音", 14, TXT, true);
        head.addView(dot); head.addView(who);
        card.addView(head);
        card.addView(text(m.time + " · " + String.format(Locale.CHINA, "%.1f 秒", m.dur), 12, GRAY, false));

        TextView pb = text("▶ 播放原声", 15, PURPLE, true);
        pb.setGravity(Gravity.CENTER);
        pb.setBackground(round(PURPLE_L, 20));
        pb.setPadding(0, dp(10), 0, dp(10));
        LinearLayout.LayoutParams pl = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        pl.topMargin = dp(8);
        pb.setLayoutParams(pl);
        pb.setOnClickListener(v -> togglePlay(pos));
        while (playBtns.size() <= pos) playBtns.add(null);
        playBtns.set(pos, pb);
        card.addView(pb);
        return card;
    }

    /* ================= 跌倒警报(app 内) ================= */

    /** 前台 3 秒一轮;退后台由 GuardService 接管。上一轮没跑完就不再排新的,网络慢也不会堆积。 */
    private final Runnable alarmPoll = new Runnable() {
        @Override public void run() {
            if (pollQueued.compareAndSet(false, true)) {
                NET.execute(() -> { try { fetchAlarms(); } finally { pollQueued.set(false); } });
            }
            refreshGuardLine();
            ui.postDelayed(this, POLL_MS);
        }
    };

    private static List<Alarm> parseAlarms(String json) throws Exception {
        JSONArray arr = new JSONObject(json).getJSONArray("alarms");
        List<Alarm> out = new ArrayList<>();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.getJSONObject(i);
            Alarm a = new Alarm();
            a.id = o.optString("id"); a.time = o.optString("time");
            a.src = o.optString("src"); a.acked = o.optBoolean("acked");
            out.add(a);
        }
        return out;
    }

    /** NET 线程:拉 /alarms;连不上就自愈找板子(找到立刻再拉一次)。结果交给主线程处理。 */
    private void fetchAlarms() {
        List<Alarm> na = null;
        try {
            na = parseAlarms(httpGet(base + "/alarms"));
        } catch (Exception e) {
            String hit = remembered;
            if (hit != null && !hit.equals(base) && ok200(hit + "/alarms")) base = hit;
            else {
                String n = subnetScan();
                if (n == null) return;                   // 冷却中 / 没找到:下轮再试
                remembered = n;
                getSharedPreferences("alarm", 0).edit().putString("base", n).apply();
                base = n;
                ui.post(() -> connState.setText("已连接老人床头 · Mocen(自动找回)"));
            }
            try { na = parseAlarms(httpGet(base + "/alarms")); } catch (Exception e2) { return; }
        }
        final List<Alarm> list = na;
        ui.post(() -> onAlarms(list));
    }

    /** 主线程:更新警报状态。别的家人点了「知道了」→ 本机也停;有新的未确认 → 拉响。 */
    private void onAlarms(List<Alarm> na) {
        alarms.clear(); alarms.addAll(na);
        if (ringingId != null) {
            Alarm cur = find(na, ringingId);
            if (cur == null || cur.acked) {             // 已在别处确认(或已从床头清掉)
                ackedHere.add(ringingId);
                ringingId = null;                        // 不清掉的话后面的新警报永远响不起来
                stopAlarmNoise();
                alarmBar.setVisibility(View.GONE);
                cancelAlarmNotice();
                toast("警报已由家人确认");
            }
            return;
        }
        for (Alarm a : na) {                             // 列表新的在前:取最新一条没确认、本机也没确认过的
            if (!a.acked && !ackedHere.contains(a.id)) { fireAlarm(a); return; }
        }
    }

    private static Alarm find(List<Alarm> l, String id) {
        for (Alarm a : l) if (a.id.equals(id)) return a;
        return null;
    }

    private void fireAlarm(Alarm a) {
        ringingId = a.id;
        alarmText.setText("⚠ 检测到老人疑似跌倒!\n" + a.time
                + (a.src.isEmpty() ? "" : " · 来源 " + a.src));
        alarmBar.setVisibility(View.VISIBLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        try {
            if (alarmSnd == null) {
                // 走"闹钟"音量通道:媒体音量调到 0 也照样响(v1.7 及以前用媒体通道,静音时警报无声)
                Uri u = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
                if (u == null) u = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
                if (u == null) u = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
                MediaPlayer mp = new MediaPlayer();
                mp.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
                mp.setDataSource(this, u);
                mp.setLooping(true);
                mp.prepare();
                mp.start();
                alarmSnd = mp;
            }
        } catch (Exception e) {}
        try {
            if (vib != null) {
                if (Build.VERSION.SDK_INT >= 26)
                    vib.vibrate(VibrationEffect.createWaveform(new long[]{0, 700, 500, 700}, 1));
                else vib.vibrate(new long[]{0, 700, 500, 700}, 1);
            }
        } catch (Exception e) {}
        Toast.makeText(this, "⚠ 跌倒警报!", Toast.LENGTH_LONG).show();
    }

    /** 「知道了」:把床头上所有还没确认的警报一起确认(旧的测试警报不会接着一条条再响),并清掉系统通知。 */
    private void ackAlarm() {
        final List<String> ids = new ArrayList<>();
        for (Alarm x : alarms) if (!x.acked) ids.add(x.id);
        if (ringingId != null && !ids.contains(ringingId)) ids.add(ringingId);
        ackedHere.addAll(ids);
        ringingId = null;
        stopAlarmNoise();
        alarmBar.setVisibility(View.GONE);
        cancelAlarmNotice();
        if (ids.isEmpty()) return;
        final String target = base;
        NET.execute(() -> {
            int failed = 0;
            for (String id : ids) {
                try {
                    HttpURLConnection c = open(target + "/alarms/ack");
                    c.setRequestMethod("POST"); c.setDoOutput(true);
                    c.setConnectTimeout(3000); c.setReadTimeout(4000);
                    c.setRequestProperty("Content-Type", "application/json");
                    OutputStream os = c.getOutputStream();
                    os.write(new JSONObject().put("id", id).toString().getBytes(StandardCharsets.UTF_8));
                    os.close();
                    int code = c.getResponseCode();
                    readAll(code >= 400 ? c.getErrorStream() : c.getInputStream());
                    c.disconnect();
                    if (code != 200) failed++;
                } catch (Exception e) { failed++; }
            }
            final int f = failed;
            if (f > 0) ui.post(() -> toast("确认没送到床头(网络问题),其他家人那里可能还在响"));
        });
    }

    private void cancelAlarmNotice() {
        try { ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).cancel(ALARM_NOTIFY_ID); }
        catch (Exception e) {}
    }

    private void stopAlarmNoise() {
        try { if (alarmSnd != null) { alarmSnd.stop(); alarmSnd.release(); } } catch (Exception e) {}
        alarmSnd = null;
        try { if (vib != null) vib.cancel(); } catch (Exception e) {}
        try { getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); } catch (Exception e) {}
    }

    /** 权限体检:通知权限(Android13+)/电池优化白名单。后台通知 nm.notify 在无通知权限时静默丢弃——
     *  前台红条不需要该权限,所以"前台能响、熄屏没影"的头号真凶就是它;缺哪样亮哪条。 */
    private void refreshPermBanner() {
        boolean need = false;
        String msg = "";
        try {
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                    != PackageManager.PERMISSION_GRANTED) {
                need = true; msg = "⚠ 未开通知权限——熄屏/退后台收不到警报提醒";
            }
        } catch (Exception e) {}
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (!pm.isIgnoringBatteryOptimizations(getPackageName())) {
                need = true;
                msg = msg.isEmpty() ? "⚠ 未设后台常驻——系统可能清理守护服务造成漏报"
                                    : "⚠ 未开通知权限+未设后台常驻——熄屏将收不到警报";
            }
        } catch (Exception e) {}
        permText.setText(msg.isEmpty() ? "⚠ 提醒权限未开全——熄屏/退后台将收不到警报" : msg);
        permBar.setVisibility(need ? View.VISIBLE : View.GONE);
        if (need && Build.VERSION.SDK_INT >= 33 && !notifAsked) {   // 自动弹一次,其余靠横幅引导
            notifAsked = true;
            try { requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 9); }
            catch (Exception e) {}
        }
        refreshGuardLine();
    }

    private void openPermSettings() {
        try {
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                    != PackageManager.PERMISSION_GRANTED)
                requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 9);
        } catch (Exception e) {}
        try {
            startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            try { startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName()))); } catch (Exception e2) {}
        }
    }

    /** 守护自检:GuardService 每次成功轮询 /alarms 都会刷新 lastPoll(首次 4s,此后 10s 一拍)。
     *  熄屏测完打开 App 一瞄:"轮询 3 秒前 ✓"=服务活着、只是通知没弹(权限/通知通道层问题);
     *  "轮询 5 分钟前 · 停了?"=熄屏时服务被杀或断网(ROM 层)。两种结论走完全不同的修复路线。 */
    private void refreshGuardLine() {
        try {
            android.content.SharedPreferences pf = getSharedPreferences("alarm", 0);
            long last = pf.getLong("lastPoll", 0L);
            String poll;
            if (last == 0L) poll = "轮询还没跑过";
            else {
                long age = (System.currentTimeMillis() - last) / 1000;
                poll = "轮询 " + age + " 秒前" + (age <= 30 ? " ✓" : " · 停了?");
            }
            boolean notif = true;
            if (Build.VERSION.SDK_INT >= 33)
                notif = checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                        == PackageManager.PERMISSION_GRANTED;
            boolean bat = true;
            try {
                android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
                bat = pm.isIgnoringBatteryOptimizations(getPackageName());
            } catch (Exception e) {}
            boolean bad = poll.contains("停了") || poll.contains("还没跑过") || !notif || !bat;
            guardLine.setText("守护: " + poll + " · 通知权限 " + (notif ? "✓" : "✗")
                    + " · 后台常驻 " + (bat ? "✓" : "✗"));
            guardLine.setTextColor(bad ? RED : GREEN);
        } catch (Exception e) {}
    }

    /* ================= 播放(v1.8:先下载后播放+全程按钮反馈) =================
     * 板子 WiFi 链路抖(实测单请求 0.3~47s 随机抖),MediaPlayer 网络流一旦卡住
     * 用户毫无感知=「点了没反应」。改为:点按→按钮立刻变「下载中…」→拿全文件
     * (超时 12s)→本地 prepare(毫秒级)→播放,按钮变「■ 停止」;完成/失败复原。
     * 永不给"点了不吭声"的机会。 */

    private static final java.util.concurrent.ExecutorService PLAY_EXEC =
            java.util.concurrent.Executors.newSingleThreadExecutor(r -> new Thread(r, "player"));
    private int playGen = 0;                     // 每次点播 +1;旧请求自动作废
    private TextView playingBtn = null;          // 正在下载/播放的那个按钮(反馈用)

    private void playBtnState(final TextView btn, final String s, final boolean busy) {
        ui.post(() -> {
            btn.setText(s);
            btn.setTextColor(busy ? 0xFFFFFFFF : PURPLE);
            btn.setBackground(round(busy ? 0xFF6C4CE0 : PURPLE_L, 20));
        });
    }

    private void resetPlayBtn() {
        final TextView b = playingBtn;
        playingBtn = null;
        if (b != null) playBtnState(b, "▶ 播放原声", false);
    }

    private void togglePlay(int pos) {
        if (pos < 0 || pos >= msgs.size()) return;
        final TextView btn = playBtns.size() > pos ? playBtns.get(pos) : null;
        final java.io.File cf = cacheOf(msgs.get(pos).id);          // v1.9:点播先看缓存
        final boolean cached = cf != null && cf.exists() && cf.length() > 1000;
        final int g = ++playGen;
        final String url = base + "/messages/" + msgs.get(pos).id + ".wav";
        final int p = pos;
        if (btn != null) playBtnState(btn, cached ? "播放中…" : "下载中…", true);  // 点按瞬间立刻有反馈
        PLAY_EXEC.execute(() -> {
            if (g != playGen) {                                     // 期间又有新点击:本请求作废
                if (btn != null) playBtnState(btn, "▶ 播放原声", false);
                return;
            }
            if (p == playingPos && player != null) {                // 同一条再点=当"停止"用
                ++playGen;                                          // 作废后续同 pos 旧任务
                releasePlayer();
                resetPlayBtn();
                return;
            }
            byte[] wav = null;
            if (cached) { try { wav = readBytes(cf); } catch (Exception e) { wav = null; } }
            if (wav == null) {
                try { wav = httpGetbin(url, 12000); }
                catch (Exception e) {
                    ui.post(() -> toast("下载失败,网络抖了,再点一次就好"));
                    resetPlayBtn();
                    return;
                }
            }
            if (g != playGen) { resetPlayBtn(); return; }           // 下载完已被新点击顶掉
            startPlay(g, wav, pos);
        });
    }

    private void startPlay(int g, byte[] wav, int pos) {
        releasePlayer();                         // 串行线程内执行
        final MediaPlayer mp = new MediaPlayer();
        try {
            java.io.File f = new java.io.File(getCacheDir(), "play.wav");
            try (java.io.FileOutputStream fo = new java.io.FileOutputStream(f)) { fo.write(wav); }
            player = mp;
            try (java.io.FileInputStream fis = new java.io.FileInputStream(f)) { mp.setDataSource(fis.getFD()); }
            mp.setOnErrorListener((m, w, x) -> true);
            mp.setOnCompletionListener(m -> {
                playingPos = -1;
                resetPlayBtn();                  // 播完自动复原按钮
            });
            mp.prepare();                        // 本地文件,毫秒级
            if (g != playGen) { releasePlayer(); return; }
            mp.start();
            playingPos = pos;
            final TextView b2 = playBtns.size() > pos ? playBtns.get(pos) : null;
            final TextView prev = playingBtn;                        // v1.9:换播另一条时,旧条按钮复原
            if (prev != null && !prev.equals(b2)) playBtnState(prev, "▶ 播放原声", false);
            if (b2 != null) { playingBtn = b2; playBtnState(b2, "■ 停止播放", true); }
            ui.post(() -> toast("▶ 播放中"));
        } catch (Exception e) {
            ui.post(() -> toast("播放失败,再点一次试试"));
            resetPlayBtn();
            releasePlayer();
        }
    }

    private void releasePlayer() {               // 只允许在 PLAY_EXEC(player)线程调用
        try { if (player != null) { player.stop(); player.release(); } } catch (Exception e) {}
        player = null; playingPos = -1;
    }

    private byte[] httpGetbin(String u, int timeoutMs) throws Exception {
        HttpURLConnection c = open(u);          // 局域网直连,不走系统代理
        c.setConnectTimeout(5000); c.setReadTimeout(timeoutMs);
        java.io.InputStream is = c.getInputStream();
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = is.read(b)) > 0) bo.write(b, 0, n);
        is.close(); c.disconnect();
        return bo.toByteArray();
    }

    private static byte[] readBytes(java.io.File f) throws Exception {   // v1.9 缓存命中时本地直读
        java.io.FileInputStream fi = new java.io.FileInputStream(f);
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = fi.read(b)) > 0) bo.write(b, 0, n);
        fi.close();
        return bo.toByteArray();
    }

    /** 试听自己刚录的(本地文件,毫秒级);没录过给提示。 */
    private void playFileOrToast() {
        final File f = getRecFile();
        if (f == null || !f.exists() || f.length() < 1000) {
            toast("还没录,先按住说话");
            return;
        }
        final int g = ++playGen;                 // 试听共用同一串行线程与作废计数
        PLAY_EXEC.execute(() -> {
            if (g != playGen) return;
            releasePlayer();
            try {
                player = new MediaPlayer();
                FileInputStream fis = new FileInputStream(f);
                player.setDataSource(fis.getFD());
                player.setOnErrorListener((m, w, x) -> true);
                player.setOnCompletionListener(mp -> ui.post(() -> {}));
                player.prepare();
                if (g != playGen) { releasePlayer(); return; }
                player.start();
            } catch (Exception e) {
                ui.post(() -> toast("试听失败"));
                releasePlayer();
            }
        });
    }

    /* ================= 录音(按下说,松手停;16k/16bit/mono) ================= */

    private static final int MAX_SEC = 60;
    private static final int SR = 16000;

    private final Runnable recTick = new Runnable() {
        @Override public void run() {
            if (!recing) return;
            timerLabel.setText(String.format(Locale.CHINA, "%.1fs",
                    (System.currentTimeMillis() - recStart) / 1000.0));
            ui.postDelayed(this, 100);
        }
    };

    private void startRec() {
        if (recing) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        PLAY_EXEC.execute(this::releasePlayer);   // 开录前把正在放的静音,串行线程内释放
        resetPlayBtn();
        synchronized (pcmBuf) { pcmBuf.reset(); }
        recFile = null;
        talkPanel.setVisibility(View.GONE);
        final AudioRecord r;
        try {
            int min = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            r = new AudioRecord(android.media.MediaRecorder.AudioSource.MIC, SR,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min * 4, 32000));
            if (r.getState() != AudioRecord.STATE_INITIALIZED) { r.release(); throw new IllegalStateException(); }
            r.startRecording();
        } catch (Exception e) {
            stateLabel.setText("麦克风打不开(可能正被别的应用占用),稍后再试");
            return;
        }
        rec = r;
        recing = true; recStart = System.currentTimeMillis();
        recThread = new Thread(() -> {
            byte[] b = new byte[3200];
            while (recing) {
                int n = r.read(b, 0, b.length);
                if (n > 0) {
                    boolean full;
                    synchronized (pcmBuf) { pcmBuf.write(b, 0, n); full = pcmBuf.size() > SR * 2 * MAX_SEC; }
                    if (full) { ui.post(() -> stopRec(false)); break; }
                } else if (n < 0) break;                 // 录音设备出错
            }
        }, "rec");
        recThread.start();
        stateLabel.setText("正在说…松手结束(最长 " + MAX_SEC + " 秒)");
        ui.post(recTick);
    }

    private void stopRec(boolean silent) {
        if (!recing) { return; }
        recing = false;
        ui.removeCallbacks(recTick);
        // 先等录音线程退出,再停 / 释放设备:不然线程可能还在 read 一个已释放的 AudioRecord
        try { if (recThread != null) recThread.join(600); } catch (InterruptedException e) {}
        try { if (rec != null) rec.stop(); } catch (Exception e) {}
        try { if (rec != null) rec.release(); } catch (Exception e) {}
        rec = null; recThread = null;
        if (silent) return;
        double secs = (System.currentTimeMillis() - recStart) / 1000.0;
        byte[] pcm;
        synchronized (pcmBuf) { pcm = pcmBuf.toByteArray(); }
        timerLabel.setText(String.format(Locale.CHINA, "%.1fs", secs));
        if (pcm.length < 8000) {                       // ≥0.25s,服务端硬要求
            stateLabel.setText("太短了,按住圆钮再说一遍");
            return;
        }
        try {
            recFile = wavFile(pcm, SR);
            talkPanel.setVisibility(View.VISIBLE);
            sendBtn.setEnabled(true);
            stateLabel.setText(String.format(Locale.CHINA, "%.0f", secs) + " 秒,试听满意就发");
        } catch (Exception e) { stateLabel.setText("录音处理失败"); }
    }

    private File getRecFile() { return recFile; }

    private void clearRec() {
        recFile = null;
        synchronized (pcmBuf) { pcmBuf.reset(); }
        talkPanel.setVisibility(View.GONE);
        timerLabel.setText("0.0s"); stateLabel.setText("按住圆钮开始,松手说完");
    }

    private File wavFile(byte[] pcm, int rate) throws Exception {
        File f = new File(getCacheDir(), "reply.wav");
        try (java.io.FileOutputStream fo = new java.io.FileOutputStream(f)) {
            int n = pcm.length;
            byte[] h = new byte[44];
            wr(h, 0, "RIFF"); wi(h, 4, 36 + n); wr(h, 8, "WAVE"); wr(h, 12, "fmt ");
            wi(h, 16, 16); si16(h, 20, 1); si16(h, 22, 1); wi(h, 24, rate); wi(h, 28, rate * 2);
            si16(h, 32, 2); si16(h, 34, 16); wr(h, 36, "data"); wi(h, 40, n);
            fo.write(h); fo.write(pcm);
        }
        return f;
    }

    private static void wr(byte[] b, int off, String s) {
        for (int i = 0; i < s.length(); i++) b[off + i] = (byte) s.charAt(i);
    }
    private static void wi(byte[] b, int off, int v) {
        b[off] = (byte) v; b[off+1] = (byte)(v >> 8); b[off+2] = (byte)(v >> 16); b[off+3] = (byte)(v >> 24);
    }
    private static void si16(byte[] b, int off, int v) { b[off] = (byte) v; b[off+1] = (byte)(v >> 8); }

    /* ================= 发送 ================= */

    private void sendRec() {
        final File f = recFile;
        if (f == null) return;
        sendBtn.setEnabled(false);                       // 防连点重复发送
        stateLabel.setText("正在发送…");
        final String target = base;
        NET.execute(() -> {
            try {
                byte[] body = new byte[(int) f.length()];
                try (FileInputStream fi = new FileInputStream(f)) {
                    new java.io.DataInputStream(fi).readFully(body);   // 一定读满
                }
                HttpURLConnection c = open(target + "/family_message");
                c.setRequestMethod("POST");
                c.setDoOutput(true); c.setConnectTimeout(6000); c.setReadTimeout(30000);
                c.setRequestProperty("Content-Type", "application/octet-stream");
                OutputStream os = c.getOutputStream(); os.write(body); os.close();
                int code = c.getResponseCode();
                InputStream is = (code >= 400) ? c.getErrorStream() : c.getInputStream();
                String resp = readAll(is);
                c.disconnect();
                JSONObject r = new JSONObject(resp);
                boolean ok = r.optBoolean("ok");
                ui.post(() -> {
                    if (ok) {
                        toast("已发出!TA 按「听留言」就能听到你啦");
                        clearRec(); listSig = ""; swTab(0);
                    } else {
                        sendBtn.setEnabled(true);
                        stateLabel.setText("发送失败:" + r.optString("error", "未知"));
                    }
                });
            } catch (Exception e) {
                ui.post(() -> {
                    sendBtn.setEnabled(true);
                    stateLabel.setText("网络失败,确认和床头同网络,再点「发给 TA」");
                });
            }
        });
    }

    private String httpGet(String u) throws Exception {
        HttpURLConnection c = open(u);
        c.setConnectTimeout(4000); c.setReadTimeout(8000);
        String s = readAll(c.getInputStream());
        c.disconnect();
        return s;
    }

    private static String readAll(InputStream is) throws Exception {
        if (is == null) return "";
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = is.read(b)) > 0) bo.write(b, 0, n);
        is.close();
        return new String(bo.toByteArray(), StandardCharsets.UTF_8);
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }

    @Override public void onRequestPermissionsResult(int code, String[] p, int[] g) {
        if (code == REQ_MIC) {
            // 授权弹窗出现时手指早已松开:不自动开录(v1.7 会一直录到 60 秒),请用户再按一次
            if (g.length > 0 && g[0] == PackageManager.PERMISSION_GRANTED)
                stateLabel.setText("已允许麦克风,按住圆钮说话");
            else toast("需要麦克风权限才能给老人留话");
        } else if (code == 9) refreshPermBanner();   // 通知权限回执后刷新提醒条
    }
}
