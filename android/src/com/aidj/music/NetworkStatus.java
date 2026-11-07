package com.aidj.music;
import org.json.*;

/** Reachability evidence, independent of WebView's navigator.onLine and VPN flags. */
final class NetworkStatus {
    private static long issued=0,completed=0,checked=0;
    private static boolean reachable=false,known=false;
    static synchronized long begin(){return ++issued;}
    static synchronized void finish(long request,boolean ok){
        if(request<completed)return;completed=request;checked=System.currentTimeMillis();reachable=ok;known=true;
    }
    static synchronized JSONObject snapshot(){JSONObject out=new JSONObject();try{out.put("known",known);out.put("reachable",reachable);out.put("checked",checked);}catch(Exception ignored){}return out;}
}
