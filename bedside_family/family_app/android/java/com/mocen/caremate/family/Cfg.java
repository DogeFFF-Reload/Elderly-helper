// 公共配置:一处改,app 生效
package com.mocen.caremate.family;

public final class Cfg {
    /** 公网中转站地址(等队友后端/云中转)。填上后 app 自动优先走它,4G 也能用。 */
    public static final String CLOUD_BASE = "";   // 例: "https://xxx.example.com"
    /** 床头手板直连(同一路由器/热点时自动可用)。
     *  v1.5:只是初始猜测——板子换网段/换 IP 也无妨,app 会扫描自愈(见 MainActivity.discoverBase)。 */
    public static final String LAN_BASE = "http://10.170.215.251:8013";
    /** 每次启动探测超时(ms)。 */
    public static final int PROBE_TIMEOUT = 2500;
}