package com.aidj.music;

import android.app.job.*;
import android.content.*;
import java.util.concurrent.*;

/** Android chooses background execution; the system download manager owns files. */
public final class LibrarySyncJob extends JobService {
    private static final int PERIODIC=4201,SOON=4202;
    private final ExecutorService executor=Executors.newSingleThreadExecutor();
    static void schedule(Context c){
        JobScheduler scheduler=(JobScheduler)c.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if(scheduler.getPendingJob(PERIODIC)==null)scheduler.schedule(new JobInfo.Builder(PERIODIC,new ComponentName(c,LibrarySyncJob.class)).setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPeriodic(15*60*1000L).setPersisted(true).build());
    }
    static void soon(Context c){
        ((JobScheduler)c.getSystemService(Context.JOB_SCHEDULER_SERVICE)).schedule(new JobInfo.Builder(SOON,new ComponentName(c,LibrarySyncJob.class)).setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setBackoffCriteria(60000,JobInfo.BACKOFF_POLICY_EXPONENTIAL).build());
    }
    @Override public boolean onStartJob(JobParameters params){executor.execute(()->{NativeDownloads.reconcile(this);boolean ok=NativeInbox.sync(this);jobFinished(params,!ok);});return true;}
    @Override public boolean onStopJob(JobParameters params){return true;}
    @Override public void onDestroy(){executor.shutdown();super.onDestroy();}
}
