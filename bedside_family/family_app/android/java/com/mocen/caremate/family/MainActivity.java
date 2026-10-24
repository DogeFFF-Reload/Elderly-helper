package com.mocen.caremate.family;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
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
import java.util.List;
import java.util.Locale;

/**
 * CareMate 家人留言 —— 原生安卓版
 * 听 TA 说:老人的语音留言列表播放(msg_*)
 * 我来说:按住说话→试听→发给 TA(fam_*)
 * 数据源:自动探测 公网中转(Cfg.CLOUD_BASE)→ 床头直连(Cfg.LAN_BASE)
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 7;
    private static final int PURPLE = 0xFF623FD2, PURPLE_L = 0xFFEDE7FB, BG = 0xFFF6F4FB,
            TXT = 0xFF2A2140, GREEN = 0xFF3BA55D, RED = 0xFFE5484D, GRAY = 0xFF9A94AC;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private String base = Cfg.LAN_BASE;          // 当前生效的数据源
    private LinearLayout pageListen, pageTalk;
    private TextView connState, tabL, tabR, stateLabel, listTip, timerLabel;
    private LinearLayout cardBox;                // 留言卡片容器(在 ScrollView 里)
    private LinearLayout talkPanel;
    private final List<Msg> msgs = new ArrayList<>();

    // 录音
    private AudioRecord rec;
    private Thread recThread;
    private volatile boolean recing = false;
    private final ByteArrayOutputStream pcmBuf = new ByteArrayOutputStream();
    private long recStart;
    private File recFile;                        // 录完的 wav
    private MediaPlayer player;                  // 播放列表/试听共用
    private int playingPos = -1;                 // 列表播放中的位置

    // 跌倒警报
    private final List<Alarm> alarms = new ArrayList<>();
    private LinearLayout alarmBar;               // 顶部红色警报条(全局)
    private TextView alarmText;
    private MediaPlayer alarmSnd;
    private Vibrator vib;
    private volatile boolean alarming = false;   // app 内警报进行中
    private String lastAckFired = "";
    private LinearLayout permBar;                // 琥珀色权限提醒条(通知权限/后台常驻)
    private TextView permText;
    private boolean notifAsked = false;          // 本次进程只自动弹一次通知权限
    private TextView guardLine;                  // 守护自检行:轮询年龄+权限状态,熄屏测试后一瞄即知服务死活

    static class Msg { String id, time; double dur; boolean own; }
    static class Alarm { String id, time, src; boolean acked; }

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        buildUi();
        probeBase();
        refresh();
        alarmInit();
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        stopRec(true);
        if (player != null) { player.release(); player = null; }
        if (recFile != null && recFile.exists()) recFile.delete();
    }

    @Override protected void onResume() {
        super.onResume();
        refreshPermBanner();
        fetchAlarms(true);
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
        okb.setAllCaps(false);
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
        pb.setAllCaps(false);
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
        connState = text("连接中…", 12, 0xD8D2F0, false);
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
        LinearLayout spacer = new LinearLayout(this); spacer.setMinimumWidth(dp(8)); spacer.setLayoutParams(
                new LinearLayout.LayoutParams(dp(8), 1));
        tabs.addView(tabL); tabs.addView(spacer); tabs.addView(tabR);
        View finalTabs = tabs;
        tabL.setOnClickListener(v -> swTab(0, finalTabs));
        tabR.setOnClickListener(v -> swTab(1, finalTabs));
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
        re.setBackground(round(0xFFFFFFFF, 22));
        re.setOnClickListener(v -> { probeBase(); refresh(); });
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
            switch (e.getAction()) {
                case 0: startRec(); return true;                       // DOWN
                case 1: case 3: stopRec(false); return true;           // UP / CANCEL
            }
            return true;
        });
        pageTalk.addView(rbtn, rl2);

        stateLabel = text("按住圆钮开始,松手说完", 14, GRAY, false);
        pageTalk.addView(stateLabel);

        talkPanel = new LinearLayout(this);
        talkPanel.setOrientation(LinearLayout.HORIZONTAL);
        talkPanel.setGravity(Gravity.CENTER);
        talkPanel.setVisibility(View.GONE);
        talkPanel.addView(btn("重录", 0xFFEEEEEE, TXT, v -> { clearRec(); stateLabel.setText("松开=说完,重录吧"); }));
        talkPanel.addView(btn("▶ 试听", 0xFFFFFFFF, PURPLE, v -> playFile(getRecFile())));
        talkPanel.addView(btn("发给 TA", GREEN, 0xFFFFFFFF, v -> sendRec()));
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

    private void swTab(int i, View tabs) {
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
        new Thread(() -> {
            String pick = null;
            if (!Cfg.CLOUD_BASE.isEmpty() && ok200(Cfg.CLOUD_BASE + "/messages")) pick = Cfg.CLOUD_BASE;
            if (pick == null && ok200(Cfg.LAN_BASE + "/messages")) pick = Cfg.LAN_BASE;
            final String f = pick;
            ui.post(() -> {
                if (f != null) { base = f; connState.setText("已连接老人床头 · Mocen"); }
                else connState.setText("没找到床头设备(和床头连同一个网络再打开)");
            });
        }).start();
    }

    private boolean ok200(String u) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
            c.setConnectTimeout(Cfg.PROBE_TIMEOUT); c.setReadTimeout(Cfg.PROBE_TIMEOUT);
            int code = c.getResponseCode();
            c.disconnect();
            return code == 200;
        } catch (Exception e) { return false; }
    }

    /* ================= 列表 ================= */

    /** 刷新列表:后台拉 JSON → 主线程重建卡片 */
    private void refresh() {
        new Thread(() -> {
            try {
                String j = httpGet(base + "/messages");
                JSONArray arr = new JSONObject(j).getJSONArray("messages");
                List<Msg> nm = new ArrayList<>();
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    Msg m = new Msg();
                    m.id = o.getString("id"); m.time = o.optString("time");
                    m.dur = o.optDouble("duration", 0);
                    m.own = m.id.startsWith("fam_");
                    nm.add(m);
                }
                ui.post(() -> { msgs.clear(); msgs.addAll(nm); fillList(); });
            } catch (Exception e) {
                ui.post(() -> {
                    connState.setText("没找到床头设备(和床头连同一个网络再打开)");
                    listTip.setText("连不上床头:手机要和床头在同一个网络,点刷新重试");
                    msgs.clear(); fillList();
                });
            }
        }).start();
    }

    private void fillList() {
        cardBox.removeAllViews();
        // —— 警报历史(红卡,置顶) ——
        if (!alarms.isEmpty()) {
            cardBox.addView(sepTitle("— 跌倒警报记录 —"));
            boolean openShown = false;
            for (Alarm a : alarms) {
                if (a.acked && !openShown) {
                    cardBox.addView(sepTitle("— 已确认 —"));
                    openShown = true;
                }
                TextView c = text((a.acked ? "✓ 已确认 · " : "⚠ 未确认 · ")
                        + a.time + " 检测到跌倒" + (a.src.isEmpty() ? "" : "(来源 " + a.src + ")"),
                        13, a.acked ? GRAY : RED, !a.acked);
                c.setBackground(round(a.acked ? 0xFFF1F0F5 : 0xFFFCE9EA, 14));
                c.setPadding(dp(14), dp(11), dp(14), dp(11));
                LinearLayout.LayoutParams cl = mp(-1, -2, 0, 0, 0, dp(8));
                cardBox.addView(c, cl);
            }
        }
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
            cardBox.addView(msgCard(m), mp(-1, -2, 0, 0, 0, dp(10)));
        }
    }

    private LinearLayout.LayoutParams mp(int w, int h, int l, int t, int r, int b) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(w, h);
        p.setMargins(l, t, r, b);
        return p;
    }

    private TextView sepTitle(String s) {
        TextView t = text(s, 12, GRAY, false);
        t.setGravity(Gravity.CENTER);
        t.setPadding(0, dp(14), 0, dp(6));
        return t;
    }

    private int countMine() { int n = 0; for (Msg m : msgs) if (m.own) n++; return n; }
    private String newestElderId() { for (Msg m : msgs) if (!m.own) return m.id; return null; }

    private View msgCard(Msg m) {
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
        int pos = msgs.indexOf(m);
        pb.setOnClickListener(v -> togglePlay(pos));
        card.addView(pb);
        return card;
    }

    /* ================= 跌倒警报(app 内) ================= */

    /** 初始化:启动守护服务 + 权限体检 + 3 秒轮询循环 */
    private void alarmInit() {
        vib = (Vibrator) getSystemService(VIBRATOR_SERVICE);
        refreshPermBanner();
        try { startForegroundService(new Intent(this, GuardService.class)); } catch (Exception e) {}
        // 3 秒一轮警报轮询(app 在前台时最快触达;关 app 后由 GuardService 接管)
        ui.postDelayed(new Runnable() {
            @Override public void run() {
                new Thread(() -> fetchAlarms(true)).start();
                ui.postDelayed(this, 3000);
            }
        }, 1500);
    }

    /** 拉 /alarms → 刷新历史 + 判定要不要拉响 app 内警报 */
    private void fetchAlarms(boolean mayFire) {
        try {
            HttpURLConnection c = (HttpURLConnection)
                    new java.net.URL(base + "/alarms").openConnection();
            c.setConnectTimeout(2500); c.setReadTimeout(2500);
            String j = readAll(c.getInputStream()); c.disconnect();
            JSONArray arr = new JSONObject(j).getJSONArray("alarms");
            List<Alarm> na = new ArrayList<>();
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                Alarm a = new Alarm();
                a.id = o.optString("id"); a.time = o.optString("time");
                a.src = o.optString("src"); a.acked = o.optBoolean("acked");
                na.add(a);
            }
            String fireId = null;
            for (Alarm a : na) if (!a.acked) { fireId = a.id; break; }
            final Alarm fire = (fireId != null && mayFire &&
                    !alarming && !fireId.equals(lastAckFired)) ? find(na, fireId) : null;
            ui.post(() -> {
                alarms.clear(); alarms.addAll(na);
                fillList();
                if (fire != null) fireAlarm(fire);
            });
        } catch (Exception e) {
            // 连不上:静默(下轮再试);若正拉响也先不关(可能网络抖)
        }
    }

    private static Alarm find(List<Alarm> l, String id) {
        for (Alarm a : l) if (a.id.equals(id)) return a;
        return null;
    }

    private void fireAlarm(Alarm a) {
        alarming = true;
        alarmText.setText("⚠ 检测到老人疑似跌倒!\n" + a.time
                + (a.src.isEmpty() ? "" : " · 来源 " + a.src));
        alarmBar.setVisibility(View.VISIBLE);
        try {
            if (alarmSnd == null) {
                alarmSnd = MediaPlayer.create(this,
                        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM));
                if (alarmSnd != null) { alarmSnd.setLooping(true); alarmSnd.start(); }
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

    private void ackAlarm() {
        Alarm a = null;
        for (Alarm x : alarms) if (!x.acked) { a = x; break; }
        final String id = (a != null) ? a.id : "";
        lastAckFired = id;
        stopAlarmNoise();
        alarmBar.setVisibility(View.GONE);
        if (!id.isEmpty()) {
            new Thread(() -> {
                try {
                    HttpURLConnection c = (HttpURLConnection)
                            new java.net.URL(base + "/alarms/ack").openConnection();
                    c.setRequestMethod("POST"); c.setDoOutput(true);
                    c.setConnectTimeout(3000); c.setReadTimeout(4000);
                    OutputStream os = c.getOutputStream();
                    os.write(("{\"id\":\"" + id + "\"}").getBytes(StandardCharsets.UTF_8));
                    os.close();
                    readAll(c.getInputStream()); c.disconnect();
                } catch (Exception e) {}
                new Thread(() -> fetchAlarms(true)).start();
            }).start();
        }
    }

    private void stopAlarmNoise() {
        alarming = false;
        try { if (alarmSnd != null) { alarmSnd.stop(); alarmSnd.release(); } } catch (Exception e) {}
        alarmSnd = null;
        try { if (vib != null) vib.cancel(); } catch (Exception e) {}
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
                    android.net.Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            try { startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    android.net.Uri.parse("package:" + getPackageName()))); } catch (Exception e2) {}
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

    /* ================= 播放 ================= */

    private void togglePlay(int pos) {
        if (pos < 0 || pos >= msgs.size()) return;
        if (playingPos == pos && player != null) {
            try { if (player.isPlaying()) { player.pause(); return; } } catch (Exception e) {}
        }
        final String url = base + "/messages/" + msgs.get(pos).id + ".wav";
        final int p = pos;
        new Thread(() -> playUrl(url, p)).start();   // MediaPlayer.prepare 走网络,必须在后台线程
    }

    private void playUrl(String url, int pos) {
        stopPlayer();
        try {
            player = new MediaPlayer();
            player.setAudioStreamType(android.media.AudioManager.STREAM_MUSIC);
            player.setDataSource(url);
            player.setOnCompletionListener(mp -> { playingPos = -1; });
            player.prepare();        // 同步,后台线程调
            player.start();
            playingPos = pos;
            ui.post(() -> toast("▶ 播放中"));
        } catch (Exception e) {
            ui.post(() -> toast("播放失败:" + e.getClass().getSimpleName()));
        }
    }

    private void playFile(final File f) {
        new Thread(() -> {
            stopPlayer();
            try {
                player = new MediaPlayer();
                FileInputStream fis = new FileInputStream(f);
                player.setDataSource(fis.getFD());
                player.setOnCompletionListener(mp -> ui.post(() -> {}));
                player.prepare();
                player.start();
            } catch (Exception e) { ui.post(() -> toast("试听失败")); }
        }).start();
    }

    private void stopPlayer() {
        try { if (player != null) { player.stop(); player.release(); } } catch (Exception e) {}
        player = null; playingPos = -1;
    }

    /* ================= 录音(按下说,松手停;16k/16bit/mono) ================= */

    private void startRec() {
        if (recing) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        stopPlayer();
        pcmBuf.reset(); recFile = null;
        final int SR = 16000;
        int min = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        rec = new AudioRecord(android.media.MediaRecorder.AudioSource.MIC, SR,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min * 4, 32000));
        rec.startRecording();
        recing = true; recStart = System.currentTimeMillis();
        recThread = new Thread(() -> {
            byte[] b = new byte[3200];
            while (recing) {
                int n = rec.read(b, 0, b.length);
                if (n > 0) { pcmBuf.write(b, 0, n); if (pcmBuf.size() > SR * 2 * MAX_SEC) { ui.post(() -> stopRec(false)); break; } }
            }
        });
        recThread.start();
        stateLabel.setText("正在说…松手结束");
        ui.post(() -> toast("● 录音中"));
    }

    private static final int MAX_SEC = 60;

    private void stopRec(boolean silent) {
        if (!recing) { return; }
        recing = false;
        try { if (rec != null) rec.stop(); } catch (Exception e) {}
        try { if (rec != null) rec.release(); } catch (Exception e) {}
        rec = null;
        double secs = (System.currentTimeMillis() - recStart) / 1000.0;
        byte[] pcm = pcmBuf.toByteArray();
        timerLabel.setText(String.format(Locale.CHINA, "%.1fs", secs));
        if (pcm.length < 8000) {                       // ≥0.25s,服务端硬要求
            stateLabel.setText("太短了,按住圆钮再说一遍");
            toast("太短了(没到 0.25 秒)");
            return;
        }
        try {
            recFile = wavFile(pcm, 16000);
            talkPanel.setVisibility(View.VISIBLE);
            stateLabel.setText(String.format(Locale.CHINA, "%.0f", secs) + " 秒,试听满意就发");
        } catch (Exception e) { stateLabel.setText("录音处理失败"); }
    }

    private void clearRec() {
        recFile = null; pcmBuf.reset(); talkPanel.setVisibility(View.GONE);
        timerLabel.setText("0.0s"); stateLabel.setText("按住圆钮开始,松手说完");
    }

    private File getRecFile() { return recFile; }

    private File wavFile(byte[] pcm, int rate) throws Exception {
        File f = new File(getCacheDir(), "reply.wav");
        java.io.FileOutputStream fo = new java.io.FileOutputStream(f);
        int n = pcm.length;
        byte[] h = new byte[44];
        wr(h, 0, "RIFF"); wi(h, 4, 36 + n); wr(h, 8, "WAVE"); wr(h, 12, "fmt ");
        wi(h, 16, 16); si16(h, 20, 1); si16(h, 22, 1); wi(h, 24, rate); wi(h, 28, rate * 2);
        si16(h, 32, 2); si16(h, 34, 16); wr(h, 36, "data"); wi(h, 40, n);
        fo.write(h); fo.write(pcm); fo.close();
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
        stateLabel.setText("正在发送…");
        new Thread(() -> {
            try {
                byte[] body = new byte[(int) f.length()];
                FileInputStream fi = new FileInputStream(f);
                new java.io.DataInputStream(fi).readFully(body);   // 一定读满
                fi.close();
                HttpURLConnection c = (HttpURLConnection) new URL(base + "/family_message").openConnection();
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
                        clearRec(); swTab(0, null);
                    } else {
                        stateLabel.setText("发送失败:" + r.optString("error", "未知"));
                    }
                });
            } catch (Exception e) {
                ui.post(() -> stateLabel.setText("网络失败,确认和床头同网络,再点「发给 TA」"));
            }
        }).start();
    }

    private String httpGet(String u) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
        c.setConnectTimeout(4000); c.setReadTimeout(8000);
        String s = readAll(c.getInputStream());
        c.disconnect();
        return s;
    }

    private static String readAll(InputStream is) throws Exception {
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
            if (g.length > 0 && g[0] == PackageManager.PERMISSION_GRANTED) startRec();
            else toast("需要麦克风权限才能给老人留话");
        } else if (code == 9) refreshPermBanner();   // 通知权限回执后刷新提醒条
    }
}