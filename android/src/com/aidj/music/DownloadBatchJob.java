package com.aidj.music;

import android.app.job.*;
import android.os.*;

public final class DownloadBatchJob extends JobService {
    private DownloadRunner.Token token;
    private final Handler main=new Handler(Looper.getMainLooper());
    @Override public boolean onStartJob(JobParameters params){
        if(NativeDownloads.paused(this)||!NativeDownloads.hasWork(this))return false;
        DownloadBatch.finishing=false;token=new DownloadRunner.Token();DownloadRunner.Token current=token;
        if(Build.VERSION.SDK_INT>=28)current.network=params.getNetwork();
        if(Build.VERSION.SDK_INT>=34&&params.isUserInitiatedJob())setNotification(params,DownloadBatch.NOTIFICATION,DownloadBatch.notification(this,null,false),JOB_END_NOTIFICATION_POLICY_DETACH);
        else DownloadBatch.progress(this,false);
        DownloadRunner.start(this,current,new DownloadRunner.Listener(){
            public void progress(){if(!current.stopped)DownloadBatch.progress(DownloadBatchJob.this,false);}
            public void finished(boolean remaining){main.post(()->{
                if(current.stopped)return;
                synchronized(NativeDownloads.class){
                    boolean work=!NativeDownloads.paused(DownloadBatchJob.this)&&NativeDownloads.hasWork(DownloadBatchJob.this);
                    DownloadBatch.finishing=true;DownloadBatch.progress(DownloadBatchJob.this,!work);jobFinished(params,false);
                    if(work)DownloadBatch.request(DownloadBatchJob.this,false);
                }
            });}
        });return true;
    }
    @Override public boolean onStopJob(JobParameters params){
        if(token!=null)token.cancel();
        if(Build.VERSION.SDK_INT>=31&&params.getStopReason()==JobParameters.STOP_REASON_USER){State.get(this).write("downloadPaused","true");return false;}
        boolean again=!NativeDownloads.paused(this)&&NativeDownloads.hasWork(this);
        if(again){DownloadRunner.phase="Продолжу загрузку автоматически";DownloadBatch.progress(this,false);}
        else ((android.app.NotificationManager)getSystemService(NOTIFICATION_SERVICE)).cancel(DownloadBatch.NOTIFICATION);
        return again;
    }
    @Override public void onDestroy(){if(token!=null)token.cancel();super.onDestroy();}
}
