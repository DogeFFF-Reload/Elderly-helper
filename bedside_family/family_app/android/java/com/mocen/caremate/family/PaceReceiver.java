package com.mocen.caremate.family;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 闹钟级拉活链的触发端:GuardService 每轮轮询都向系统挂一个 10 秒后的 setAlarmClock 精确闹钟,
 * 由系统内核负责计时与触发(进程死了也照发)。这里收到闹钟就把守护服务重新拉起——
 * 闹钟/电话类 App 的标准存活方式,用于对抗 ROM 的熄屏杀进程/冻结。
 */
public class PaceReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent i) {
        try { c.startForegroundService(new Intent(c, GuardService.class)); }
        catch (Throwable t) {
            try { c.startService(new Intent(c, GuardService.class)); } catch (Throwable t2) {}
        }
    }
}