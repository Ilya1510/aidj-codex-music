package com.aidj.music;

import android.app.*;
import android.app.job.*;
import android.content.*;
import android.os.*;
import org.json.*;

/** One silent notification and one OS-owned task for the whole queue. */
final class DownloadBatch {
    static volatile boolean finishing=false;
    static final int JOB=4301,NOTIFICATION=102;
    private static boolean channelReady=false;
    private static final String CHANNEL="aidj-downloads-silent-v1";
    static synchronized void request(Context c,boolean user){
        if(NativeDownloads.paused(c)||!NativeDownloads.hasWork(c))return;
        JobScheduler scheduler=(JobScheduler)c.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if(Build.VERSION.SDK_INT<34&&user){
            if(DownloadService.running)return;
            try{scheduler.cancel(JOB);c.startForegroundService(new Intent(c,DownloadService.class));return;}catch(RuntimeException ignored){}
        }
        if(DownloadService.running)return;
        JobInfo old=scheduler.getPendingJob(JOB);
        if(old!=null&&!finishing&&(!user||old.getExtras().getBoolean("user")))return;
        boolean initiated=user&&Build.VERSION.SDK_INT>=34;
        if(!schedule(c,scheduler,initiated)&&initiated)schedule(c,scheduler,false);
    }
    private static boolean schedule(Context c,JobScheduler scheduler,boolean user){
        try{
            PersistableBundle extras=new PersistableBundle();extras.putBoolean("user",user);extras.putLong("request",System.currentTimeMillis());
            JobInfo.Builder builder=new JobInfo.Builder(JOB,new ComponentName(c,DownloadBatchJob.class)).setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(true).setBackoffCriteria(10000,JobInfo.BACKOFF_POLICY_EXPONENTIAL).setExtras(extras);
            if(user&&Build.VERSION.SDK_INT>=34){
                long bytes=0;for(String state:new String[]{"active","pending","retry"}){JSONArray rows=State.get(c).list(state);for(int i=0;i<rows.length();i++)bytes+=rows.optJSONObject(i).optJSONObject("track").optLong("bytes");}
                builder.setUserInitiated(true).setEstimatedNetworkBytes(bytes,0);
            }
            boolean accepted=scheduler.schedule(builder.build())==JobScheduler.RESULT_SUCCESS;if(accepted)finishing=false;return accepted;
        }catch(RuntimeException ignored){return false;}
    }
    static void stop(Context c){DownloadRunner.stop();((JobScheduler)c.getSystemService(Context.JOB_SCHEDULER_SERVICE)).cancel(JOB);c.stopService(new Intent(c,DownloadService.class));((NotificationManager)c.getSystemService(Context.NOTIFICATION_SERVICE)).cancel(NOTIFICATION);}
    static Notification notification(Context c,JSONObject state,boolean done){
        NotificationManager manager=(NotificationManager)c.getSystemService(Context.NOTIFICATION_SERVICE);
        synchronized(DownloadBatch.class){if(!channelReady){
            NotificationChannel channel=new NotificationChannel(CHANNEL,"Скачивание музыки",NotificationManager.IMPORTANCE_LOW);
            channel.setSound(null,null);channel.enableVibration(false);channel.setVibrationPattern(new long[]{0});channel.setShowBadge(false);manager.createNotificationChannel(channel);channelReady=true;
        }}
        PendingIntent open=PendingIntent.getActivity(c,102,new Intent(c,MainActivity.class),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder builder=new Notification.Builder(c,CHANNEL).setSmallIcon(R.drawable.ic_music).setContentIntent(open)
            .setContentTitle(done?"Скачивание завершено":"Скачивание музыки").setOnlyAlertOnce(true).setDefaults(0).setSound(null).setVibrate(new long[]{0})
            .setShowWhen(false).setCategory(Notification.CATEGORY_PROGRESS).setVisibility(Notification.VISIBILITY_PRIVATE).setOngoing(!done).setAutoCancel(done);
        if(state!=null){
            int completed=state.optInt("completed"),total=state.optInt("total");String text=completed+" / "+total;
            if(!state.isNull("error"))text+=" · "+state.optString("error");
            else if(!done&&!state.optString("phase").isEmpty())text+=" · "+state.optString("phase");
            builder.setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text));
            if(!done)builder.setProgress(100,state.optInt("percent"),state.optLong("bytesTotal")==0);
        }else builder.setContentText("Подготавливаю очередь").setProgress(0,0,true);
        if(!done){
            builder.addAction(new Notification.Action.Builder(android.R.drawable.ic_media_pause,"Пауза",action(c,"pause",103)).build());
            builder.addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel,"Отменить",action(c,"cancel",104)).build());
        }
        return builder.build();
    }
    private static PendingIntent action(Context c,String action,int code){return PendingIntent.getBroadcast(c,code,new Intent(c,DownloadActions.class).setAction(action),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);}
    static void progress(Context c,boolean done){try{((NotificationManager)c.getSystemService(Context.NOTIFICATION_SERVICE)).notify(NOTIFICATION,notification(c,NativeDownloads.snapshot(c),done));}catch(Exception ignored){}}
}
