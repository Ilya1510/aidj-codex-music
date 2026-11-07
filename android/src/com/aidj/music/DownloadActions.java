package com.aidj.music;

import android.content.*;

public final class DownloadActions extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent){
        PendingResult result=goAsync();Context app=context.getApplicationContext();
        new Thread(()->{try{
            if("cancel".equals(intent.getAction()))NativeDownloads.cancel(app);
            else if("pause".equals(intent.getAction()))NativeDownloads.setPaused(app,true);
            else if("resume".equals(intent.getAction()))NativeDownloads.setPaused(app,false);
        }finally{result.finish();}},"aidj-download-action").start();
    }
}
