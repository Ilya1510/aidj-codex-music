package com.aidj.music;
import android.app.DownloadManager;
import android.content.*;
public final class DownloadReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent){
        if(!DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(intent.getAction()))return;
        PendingResult pending=goAsync();Context app=context.getApplicationContext();
        new Thread(()->{try{NativeDownloads.reconcile(app);LibrarySyncJob.soon(app);}finally{pending.finish();}},"aidj-download-complete").start();
    }
}
