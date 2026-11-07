package com.aidj.music;

import java.io.*;
import java.net.*;

public final class DownloadTransferProbe {
    public static void main(String[] args)throws Exception{
        if("policy".equals(args[0])){
            if(DownloadPolicy.retryDelay(1)!=2000||DownloadPolicy.retryDelay(100)!=300000)throw new AssertionError("bounded backoff");
            if(DownloadPolicy.permanent(503,100)||DownloadPolicy.permanent(429,100))throw new AssertionError("transient failures must remain automatic");
            if(!DownloadPolicy.permanent(404,1)||!DownloadPolicy.permanent(422,3)||DownloadPolicy.permanent(422,2))throw new AssertionError("per-file failures");
            System.out.println("POLICY_OK");return;
        }
        final long stopAfter=Long.parseLong(args[4]);
        DownloadTransfer.Control control=new DownloadTransfer.Control(){
            boolean stopped=false;
            public boolean cancelled(){return stopped;}
            public void connection(HttpURLConnection connection){}
            public void progress(long bytes){if(stopAfter>0&&bytes>=stopAfter)stopped=true;}
        };
        try{
            DownloadTransfer.fetch(new URL(args[0]),"aidj_session=test-only",new File(args[1]),Long.parseLong(args[2]),args[3],control);
            System.out.println("VERIFIED");
        }catch(InterruptedIOException stopped){System.out.println("INTERRUPTED");}
        catch(DownloadTransfer.Failure failure){System.out.println("FAILURE:"+failure.code);}
        catch(IOException retry){System.out.println("RETRY");}
    }
}
