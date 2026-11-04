package com.mocen.caremate;

import android.view.View;
import android.widget.EditText;
import android.widget.Toast;

/**
 * 「进入家人端」键的回调（顶级类而非匿名——d8 dev 版对 $1 类会 NPE，见 MainActivity 注）。
 * 地址规整：去协议头、去尾斜杠；必须 host:port 都在才给过。
 */
public class ConnectListener implements View.OnClickListener {
  private final MainActivity act;
  private final android.content.SharedPreferences sp;
  private final EditText in;

  ConnectListener(MainActivity act, android.content.SharedPreferences sp, EditText in) {
    this.act = act;
    this.sp = sp;
    this.in = in;
  }

  @Override public void onClick(View v) {
    String a = in.getText().toString().trim()
        .replace("http://", "").replace("https://", "").replaceAll("/+$", "");
    String[] parts = a.split(":");
    if (a.length() > 5 && parts.length == 2 && parts[0].contains(".")
        && parts[1].matches("\\d+")) {
      sp.edit().putString("home", a).apply();
      act.finish();
      act.startActivity(act.getIntent());
    } else {
      Toast.makeText(act, "地址要带端口。示例：192.168.1.5:8000", Toast.LENGTH_LONG).show();
    }
  }
}
