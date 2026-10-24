package com.mocen.caremate;

import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;

/**
 * 麦克风门卫：家属 App 里只有一个权限值得开——「用声音回她一句」的录音。
 * 网页申请的若正是 audio capture 且仅此一项 → 放行；其余一律拒。
 * 这是 Privacy 闸门的 App 侧（主脑侧的对应物是 /api/privacy 仪表盘）。
 */
public class MicChromeClient extends WebChromeClient {
  @Override
  public void onPermissionRequest(PermissionRequest request) {
    if (request != null && request.getResources() != null
        && request.getResources().length == 1
        && PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(request.getResources()[0])) {
      request.grant(request.getResources());
    } else if (request != null) {
      request.deny();
    }
  }
}
