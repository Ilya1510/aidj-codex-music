package com.aidj.music;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.*;
import android.webkit.*;
import android.widget.*;
import android.view.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

public final class MainActivity extends Activity {
    private WebView web;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final ExecutorService executor=Executors.newFixedThreadPool(2);
    private final ExecutorService apiExecutor=Executors.newFixedThreadPool(2);
    private boolean visible=false;private static volatile boolean foreground=false;
    static boolean isVisible(){return foreground;}
    private final java.util.concurrent.atomic.AtomicBoolean publishing=new java.util.concurrent.atomic.AtomicBoolean(false);
    private long lastSync=0;private volatile String lastPlayer="";private volatile boolean playerServiceSeen=false;
    private final RemixPreview remixPreview=new RemixPreview();
    private static final String ORIGIN="https://app.aidj.local";
    private final Runnable poll=new Runnable(){public void run(){if(!visible)return;publish();if(SystemClock.elapsedRealtime()-lastSync>20000){lastSync=SystemClock.elapsedRealtime();NativeInbox.request(MainActivity.this);}main.postDelayed(this,1000);}};

    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);getWindow().setStatusBarColor(Color.rgb(20,21,20));getWindow().setNavigationBarColor(Color.rgb(20,21,20));
        if(Build.VERSION.SDK_INT>=30)getWindow().setDecorFitsSystemWindows(false);
        else getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE|View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN|View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},1);
        FrameLayout frame=new FrameLayout(this);frame.setBackgroundColor(Color.rgb(20,21,20));
        web=new WebView(this);web.setBackgroundColor(Color.rgb(20,21,20));
        frame.addView(web,new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT,FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(frame);
        frame.setOnApplyWindowInsetsListener((v,insets)->{
            if(Build.VERSION.SDK_INT>=30){android.graphics.Insets bars=insets.getInsets(WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout());int bottom=Math.max(bars.bottom,insets.getInsets(WindowInsets.Type.ime()).bottom);v.setPadding(bars.left,bars.top,bars.right,bottom);return WindowInsets.CONSUMED;}
            int top=insets.getSystemWindowInsetTop(),bottom=insets.getSystemWindowInsetBottom();
            if(Build.VERSION.SDK_INT>=28&&insets.getDisplayCutout()!=null){top=Math.max(top,insets.getDisplayCutout().getSafeInsetTop());bottom=Math.max(bottom,insets.getDisplayCutout().getSafeInsetBottom());}
            v.setPadding(insets.getSystemWindowInsetLeft(),top,insets.getSystemWindowInsetRight(),bottom);return insets.consumeSystemWindowInsets();
        });
        frame.post(frame::requestApplyInsets);
        WebSettings settings=web.getSettings();settings.setJavaScriptEnabled(true);settings.setDomStorageEnabled(true);settings.setAllowFileAccess(false);settings.setAllowContentAccess(false);settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);settings.setMediaPlaybackRequiresUserGesture(true);
        web.addJavascriptInterface(new Bridge(),"AndroidMusic");
        web.setWebViewClient(new WebViewClient(){
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request){
                Uri uri=request.getUrl();String path=uri.getPath();
                if(!"app.aidj.local".equals(uri.getHost())||!"https".equals(uri.getScheme())||!"GET".equals(request.getMethod()))return blocked();
                if(path==null||path.equals("/"))path="/index.html";
                Set<String> allowed=new HashSet<>(Arrays.asList("/index.html","/app.css","/app.mjs","/native.mjs","/personal-playlists.mjs","/web-remix.mjs","/fixed-blend.mjs","/playlist-changes.mjs","/transfer.mjs","/segments.mjs","/playback.mjs","/dj.mjs","/ratings.mjs","/reorder.mjs","/player.mjs","/storage.mjs","/icon.svg","/manifest.webmanifest","/telegram-web-app.js"));
                if(!allowed.contains(path))return blocked();
                try{
                    String mime=path.endsWith(".mjs")||path.endsWith(".js")?"text/javascript":path.endsWith(".css")?"text/css":path.endsWith(".svg")?"image/svg+xml":path.endsWith(".webmanifest")?"application/manifest+json":"text/html";
                    Map<String,String> headers=new HashMap<>();headers.put("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'");headers.put("X-Content-Type-Options","nosniff");
                    return new WebResourceResponse(mime,"UTF-8",200,"OK",headers,getAssets().open("web"+path));
                }catch(Exception e){return blocked();}
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){
                Uri uri=request.getUrl();if("app.aidj.local".equals(uri.getHost()))return false;
                openExternal(uri.toString());return true;
            }
        });
        LibrarySyncJob.schedule(this);web.loadUrl(ORIGIN+"/");handleIntent(getIntent());executor.execute(()->NativeDownloads.reconcile(this));
    }
    private WebResourceResponse blocked(){return new WebResourceResponse("text/plain","UTF-8",404,"Not Found",Collections.emptyMap(),new ByteArrayInputStream(new byte[0]));}
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);handleIntent(intent);}
    private void handleIntent(Intent intent){
        Uri uri=intent.getData();if(uri==null||!"aidjmusic".equals(uri.getScheme())||!"connect".equals(uri.getHost()))return;
        String server=uri.getQueryParameter("server"),code=uri.getQueryParameter("code");intent.setData(null);
        try{
            server=Api.validate(server);if(code==null||!code.matches("[A-Za-z0-9_-]{32,100}"))throw new Exception();
            final String target=server,key=code;
            Runnable connect=()->executor.execute(()->{
                try{JSONObject body=new JSONObject();body.put("loginCode",key);JSONObject response=Api.requestTo(this,target,"/api/auth",body,true);
                    if(response.getInt("status")!=200)throw new Exception();
                    NativeDownloads.reconcile(this);main.post(()->{web.reload();Toast.makeText(this,"Подключено",Toast.LENGTH_SHORT).show();});
                }catch(Exception e){main.post(()->new AlertDialog.Builder(this).setMessage("Ссылка устарела или сервер недоступен. Открой новую через Telegram: Музыка → Настройки → Подключить Android-приложение.").setPositiveButton("ОК",null).show());}
            });
            if(!target.equals(Api.base(this)))new AlertDialog.Builder(this).setTitle("Подключить сервер?").setMessage(Uri.parse(target).getHost()).setPositiveButton("Подключить",(d,w)->connect.run()).setNegativeButton("Отмена",null).show();else connect.run();
        }catch(Exception e){Toast.makeText(this,"Неверная ссылка подключения",Toast.LENGTH_LONG).show();}
    }
    void openExternal(String value){
        try{Uri uri=Uri.parse(value);if(!"https".equals(uri.getScheme()))return;startActivity(new Intent(Intent.ACTION_VIEW,uri));}catch(Exception e){Toast.makeText(this,"Не удалось открыть ссылку",Toast.LENGTH_SHORT).show();}
    }
    @Override protected void onResume(){super.onResume();visible=true;foreground=true;State.get(this).refreshFiles();lastSync=-20001;main.post(poll);executor.execute(()->NativeDownloads.reconcile(this));}
    @Override protected void onPause(){visible=false;foreground=false;main.removeCallbacks(poll);super.onPause();}
    @Override protected void onDestroy(){visible=false;foreground=false;main.removeCallbacks(poll);web.removeJavascriptInterface("AndroidMusic");web.destroy();web=null;executor.shutdown();apiExecutor.shutdown();super.onDestroy();}
    @Override public void onBackPressed(){web.evaluateJavascript("window.__aidjBack ? window.__aidjBack() : false",value->{if(!"true".equals(value))super.onBackPressed();});}
    private JSONObject state(){return state(true);}
    private JSONObject state(boolean includePreview){
        JSONObject value=new JSONObject();try{
            if(lastPlayer.isEmpty()){JSONObject p=new JSONObject();JSONObject q=new JSONObject(State.get(this).read("nativeQueue","{}"));p.put("queue",PlaybackService.lightQueue(q));p.put("paused",true);p.put("time",q.optDouble("time",0));p.put("duration",0);lastPlayer=p.toString();}
            JSONObject player=new JSONObject(lastPlayer);
            if(includePreview){JSONObject preview=remixPreview.plan(State.get(this),player.optJSONObject("queue"));if(preview!=null)player.put("remixPreview",preview);}
            value.put("player",player);
            value.put("access",new JSONObject(State.get(this).read("access","{}")));
            value.put("downloads",NativeDownloads.snapshot(this));value.put("files",State.get(this).files());value.put("versionCode",BuildConfig.VERSION_CODE);value.put("server",Api.base(this));
            value.put("libraryRevision",State.get(this).read("libraryRevision",""));value.put("network",NetworkStatus.snapshot().put("connected",DownloadRunner.network(this)));
            if(ImportService.instance!=null)value.put("transfer",ImportService.instance.snapshot());
        }catch(Exception ignored){}return value;
    }
    private void publish(){
        if(!publishing.compareAndSet(false,true))return;
        PlaybackService service=PlaybackService.instance;if(service!=null){lastPlayer=service.snapshot(true).toString();playerServiceSeen=true;}else if(playerServiceSeen){lastPlayer="";playerServiceSeen=false;}
        executor.execute(()->{try{
            String value=state().toString();main.post(()->{if(web!=null&&visible&&!isDestroyed())web.evaluateJavascript("window.__aidjNativeState && window.__aidjNativeState("+value+")",null);});
        }finally{publishing.set(false);}});
    }
    private void reply(String id,JSONObject result){main.post(()->{if(!isFinishing()&&!isDestroyed())web.evaluateJavascript("window.__aidjNativeReply && window.__aidjNativeReply("+JSONObject.quote(id)+","+result.toString()+")",null);});}
    private class Bridge {
        @JavascriptInterface public String readMeta(String key){return key!=null&&key.matches("[A-Za-z0-9_-]{1,100}")?State.get(MainActivity.this).read("web_"+key,"null"):"null";}
        @JavascriptInterface public void writeMeta(String key,String value){if(key!=null&&key.matches("[A-Za-z0-9_-]{1,100}")&&value!=null&&value.length()<=(("catalog".equals(key)||"player".equals(key))?8:2)*1024*1024)State.get(MainActivity.this).write("web_"+key,value);}
        @JavascriptInterface public String files(){return State.get(MainActivity.this).files().toString();}
        @JavascriptInterface public String snapshot(){return state(false).toString();}
        @JavascriptInterface public int version(){return BuildConfig.VERSION_CODE;}
        @JavascriptInterface public void open(String value){main.post(()->openExternal(value));}
        @JavascriptInterface public void request(String id,String payload){
            if(id==null||!id.matches("[0-9]{1,12}")||payload==null)return;
            if(payload.length()>8*1024*1024){try{reply(id,new JSONObject().put("error","Слишком большая очередь. Выбери меньше песен."));}catch(Exception ignored){}return;}
            ExecutorService target=executor;
            try{if("api".equals(new JSONObject(payload).optString("type")))target=apiExecutor;}catch(Exception ignored){}
            target.execute(()->{
                JSONObject result=new JSONObject();
                try{
                    JSONObject data=new JSONObject(payload);String type=data.getString("type");
                    if("api".equals(type))result=Api.request(MainActivity.this,data.getString("path"),data.isNull("body")?null:data.getJSONObject("body"));
                    else if("download".equals(type)){NativeDownloads.addUser(MainActivity.this,data.getJSONArray("tracks"));result.put("ok",true);}
                    else if("downloadToggle".equals(type)){NativeDownloads.toggle(MainActivity.this);result.put("ok",true);}
                    else if("downloadPause".equals(type)){NativeDownloads.setPaused(MainActivity.this,data.getBoolean("paused"));result.put("ok",true);}
                    else if("downloadCancel".equals(type)){NativeDownloads.cancel(MainActivity.this);result.put("ok",true);}
                    else if("removeFile".equals(type)){String md5=data.getString("md5");if(!md5.matches("[a-f0-9]{32}"))throw new Exception();State.get(MainActivity.this).remove(md5);result.put("ok",true);}
                    else if("importStart".equals(type)){
                        main.post(()->{try{startForegroundService(new Intent(MainActivity.this,ImportService.class));}catch(Exception e){Toast.makeText(MainActivity.this,"Открой приложение и повтори перенос.",Toast.LENGTH_LONG).show();}});
                        result.put("ok",true);
                    }
                    else if("queue".equals(type)||"play".equals(type)){
                        JSONObject queue=data.getJSONObject("queue");if(queue.optJSONArray("entries")==null||queue.getJSONArray("entries").length()>5000)throw new Exception();
                        final boolean play="play".equals(type),replace=data.optBoolean("replaceQueue");
                        main.post(()->{
                            JSONObject answer=new JSONObject();
                            try{
                                PlaybackService service=PlaybackService.instance;
                                if(service!=null){
                                    boolean accepted=service.applyQueue(queue,replace);
                                    // Resume targets the live song even if the submitted UI
                                    // snapshot predates its automatic transition.
                                    if(play&&(accepted||!replace))service.play();
                                    answer.put("queueAccepted",accepted).put("requestedUiRevision",queue.optLong("uiRevision")).put("player",service.snapshot(true));
                                }else{
                                    JSONObject saved=new JSONObject(State.get(MainActivity.this).read("nativeQueue","{}"));
                                    long epoch=Math.max(0,saved.optLong("playbackEpoch"));Object raw=queue.opt("playbackEpoch");
                                    boolean matching=raw instanceof Number&&Double.isFinite(((Number)raw).doubleValue())&&((Number)raw).doubleValue()==epoch;
                                    boolean accepted=queue.optLong("uiRevision")>=saved.optLong("uiRevision")&&(replace||queue.optInt("queueProtocol")==0||matching);
                                    JSONObject selected=saved;
                                    if(accepted){
                                        if(!saved.optString("currentId").equals(queue.optString("currentId"))&&epoch<Long.MAX_VALUE)epoch++;
                                        queue.put("playbackEpoch",epoch);State.get(MainActivity.this).write("nativeQueue",queue.toString());selected=queue;lastPlayer="";
                                    }
                                    JSONObject player=new JSONObject().put("queue",PlaybackService.lightQueue(selected)).put("paused",!play).put("time",selected.optDouble("time",0)).put("duration",0);
                                    answer.put("queueAccepted",accepted).put("requestedUiRevision",queue.optLong("uiRevision")).put("player",player);
                                    if(play&&(accepted||!replace))startForegroundService(new Intent(MainActivity.this,PlaybackService.class).putExtra("action","play"));
                                }
                                answer.put("ok",true);
                            }catch(Exception e){try{answer.put("error","Не удалось обновить очередь. Попробуй ещё раз.");}catch(Exception ignored){}}
                            reply(id,answer);publish();
                        });return;
                    }else if("metadata".equals(type)){
                        JSONObject patch=data.getJSONObject("metadata");
                        if(patch.optJSONArray("entries")==null||patch.getJSONArray("entries").length()>5000)throw new Exception();
                        main.post(()->{
                            JSONObject answer=new JSONObject();
                            try{
                                PlaybackService service=PlaybackService.instance;
                                if(service!=null){service.refreshMetadata(patch);answer.put("player",service.snapshot(true));}
                                answer.put("ok",true);
                            }catch(Exception e){try{answer.put("error","Не удалось обновить данные песен.");}catch(Exception ignored){}}
                            reply(id,answer);publish();
                        });return;
                    }else if("pause".equals(type)||"seek".equals(type)||"next".equals(type)||"previous".equals(type)){
                        main.post(()->{PlaybackService s=PlaybackService.instance;if(s!=null){if("pause".equals(type))s.pause();else if("next".equals(type))s.skip(1);else if("previous".equals(type))s.skip(-1);else s.seek((long)(data.optDouble("time")*1000));}else if("next".equals(type)||"previous".equals(type)){startForegroundService(new Intent(MainActivity.this,PlaybackService.class).putExtra("action",type));}});result.put("ok",true);
                    }else throw new Exception();
                }catch(Exception e){try{result.put("error","Не удалось выполнить действие. Проверь сеть или подключение к боту.");}catch(Exception ignored){}}
                reply(id,result);main.post(MainActivity.this::publish);
            });
        }
    }
}
