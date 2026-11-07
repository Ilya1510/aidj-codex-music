package com.aidj.music;

import android.app.*;
import android.content.*;
import android.os.*;
import android.net.*;

/** User-started foreground transfer, with JobScheduler for deferred recovery. */
public final class DownloadService extends Service {
    static volatile boolean running=false;
    private volatile boolean alive=true;
    private ConnectivityManager connectivity;
    private final ConnectivityManager.NetworkCallback networkCallback=new ConnectivityManager.NetworkCallback(){
        @Override public void onCapabilitiesChanged(Network network,NetworkCapabilities capabilities){
            updateWake(capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET));
        }
    };
    private DownloadRunner.Token token;private PowerManager.WakeLock wake;
    private final Handler main=new Handler(Looper.getMainLooper());
    private synchronized void updateWake(boolean online){
        if(!alive||wake==null)return;if(online){if(!wake.isHeld())wake.acquire(10*60*1000L);}else if(wake.isHeld())wake.release();
    }
    @Override public void onCreate(){
        super.onCreate();running=true;DownloadBatch.serviceStarting=false;startForeground(DownloadBatch.NOTIFICATION,DownloadBatch.notification(this,null,false));
        wake=((PowerManager)getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"aidj:playlist-download");wake.setReferenceCounted(false);wake.acquire(10*60*1000L);
        connectivity=(ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);connectivity.registerDefaultNetworkCallback(networkCallback);
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(token!=null)return START_STICKY;
        token=new DownloadRunner.Token();DownloadRunner.Token current=token;
        DownloadRunner.start(this,current,new DownloadRunner.Listener(){
            public void progress(){
                if(current.stopped)return;
                updateWake(DownloadRunner.network(DownloadService.this));
                DownloadBatch.progress(DownloadService.this,false);
            }
            public void finished(boolean remaining){main.post(()->{
                if(current.stopped)return;
                if(!NativeDownloads.paused(DownloadService.this)&&NativeDownloads.hasWork(DownloadService.this)){
                    token=null;main.postDelayed(()->{if(alive&&token==null)onStartCommand(null,0,0);},1000);return;
                }
                DownloadBatch.progress(DownloadService.this,!NativeDownloads.hasWork(DownloadService.this));stopForeground(STOP_FOREGROUND_DETACH);stopSelf();
            });}
        });return START_STICKY;
    }
    @Override public void onTimeout(int startId,int type){if(token!=null)token.cancel();stopForeground(STOP_FOREGROUND_REMOVE);stopSelf();DownloadBatch.request(this,false);}
    @Override public void onDestroy(){
        running=false;synchronized(this){alive=false;if(token!=null)token.cancel();if(wake!=null&&wake.isHeld())wake.release();}
        if(connectivity!=null)try{connectivity.unregisterNetworkCallback(networkCallback);}catch(RuntimeException ignored){}
        super.onDestroy();if(!NativeDownloads.paused(this)&&NativeDownloads.hasWork(this))DownloadBatch.request(this,false);
    }
    @Override public IBinder onBind(Intent intent){return null;}
}
