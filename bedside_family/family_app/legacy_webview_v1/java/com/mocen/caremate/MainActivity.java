package com.mocen.caremate;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * CareMate 家人端 · 安卓壳（把"家里的窗口"真正装进口袋——是要 App，就给 App）。
 *
 * 设计立场（答辩背书）：
 *  1. 壳只负责"一口锅"：留言簿/看板/紧急通知全来自家里主脑，App 不含业务逻辑；
 *  2. 首次使用填一次家里地址，存在手机本地——不注册、不登录、不连任何云；
 *  3. 录音权限只为"用声音回她一句"这一个功能申请（MicChromeClient 里白名单放行）；
 *  4. 无 SDK、无统计、无第三方库——数据从没出过家门内网。
 *
 * 工程注记：全文件零匿名内部类（lambda + 顶级类 MicChromeClient）——
 * build-tools 34.0.0 的 d8 是 dev 构建会在匿名类 EnclosingMethod 上 NPE，
 * 记在注释里，省得明天现场重新踩一遍。
 */
public class MainActivity extends Activity {
  private WebView web;

  @Override
  protected void onCreate(Bundle savedInstanceState) {
    super.onCreate(savedInstanceState);
    SharedPreferences sp = getSharedPreferences("caremate", Context.MODE_PRIVATE);
    String home = sp.getString("home", "");
    if (home.isEmpty()) {
      buildSetup(sp);
    } else {
      openHome(home);
    }
  }

  /** 原生首屏：填一次家里主脑地址（床头贴条），存 SharedPreferences。 */
  private void buildSetup(final SharedPreferences sp) {
    float d = getResources().getDisplayMetrics().density;
    int pad = (int) (24 * d);
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    root.setPadding(pad, pad * 3, pad, pad / 2);
    root.setBackgroundColor(0xFF172A47);

    TextView title = new TextView(this);
    title.setText("CareMate\n家人端 · App");
    title.setTextColor(0xFFFFFFFF);
    title.setTextSize(24);
    title.setPadding(0, 0, 0, (int) (10 * d));
    root.addView(title);

    TextView hint = new TextView(this);
    hint.setText("第一次使用：输入家里主脑的地址（床头贴条上写的，形如 192.168.1.5:8000）。\n\n"
        + "地址只存在这台手机里，不经过任何第三方；以后换 Wi-Fi 也能重填，"
        + "长按 App 图标进应用信息、清空数据即可。");
    hint.setTextColor(0xCCFFFFFF);
    hint.setTextSize(14);
    root.addView(hint);

    final EditText in = new EditText(this);
    in.setHint("192.168.1.5:8000");
    in.setTextSize(16);
    root.addView(in);

    Button go = new Button(this);
    go.setText("进入家人端");
    go.setBackgroundColor(0xFF16A34A);
    go.setTextColor(0xFFFFFFFF);
    go.setOnClickListener(new ConnectListener(this, sp, in));
    root.addView(go);
    setContentView(root);
  }

  private void openHome(String host) {
    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
        != PackageManager.PERMISSION_GRANTED) {
      requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 7);
    }
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    web = new WebView(this);
    WebSettings s = web.getSettings();
    s.setJavaScriptEnabled(true);
    s.setDomStorageEnabled(true);
    s.setMediaPlaybackRequiresUserGesture(false);
    web.setWebViewClient(new WebViewClient());
    web.setWebChromeClient(new MicChromeClient());
    root.addView(web);
    setContentView(root);
    web.loadUrl("http://" + host + "/ui/family");
  }

  @Override
  public boolean onKeyDown(int keyCode, KeyEvent event) {
    if (web != null && keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) {
      web.goBack();
      return true;
    }
    return super.onKeyDown(keyCode, event);
  }
}
