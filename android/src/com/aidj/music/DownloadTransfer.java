package com.aidj.music;

import java.io.*;
import java.net.*;
import java.security.MessageDigest;
import java.util.regex.*;

/** Resumable bounded streaming. Only a fully verified part may be committed. */
final class DownloadTransfer {
    interface Control {
        boolean cancelled();
        default HttpURLConnection open(URL url)throws IOException{return (HttpURLConnection)url.openConnection();}
        void connection(HttpURLConnection value);
        void progress(long bytes);
    }
    static final class Failure extends IOException {
        final int code;
        Failure(int code,String message){super(message);this.code=code;}
    }
    static void check(Control control)throws InterruptedIOException {
        if(control.cancelled()||Thread.currentThread().isInterrupted())throw new InterruptedIOException("Stopped");
    }
    static boolean verify(File file,long expected,String digest){
        if(!file.isFile()||file.length()!=expected)return false;
        try(InputStream in=new BufferedInputStream(new FileInputStream(file))){
            MessageDigest hash=MessageDigest.getInstance("MD5");byte[] buffer=new byte[65536];int n;
            while((n=in.read(buffer))!=-1)hash.update(buffer,0,n);
            StringBuilder value=new StringBuilder();for(byte b:hash.digest())value.append(String.format("%02x",b&255));
            return value.toString().equals(digest);
        }catch(Exception ignored){return false;}
    }
    static void fetch(URL url,String cookie,File part,long expected,String digest,Control control)throws IOException {
        check(control);
        if(expected<=0||expected>40L*1024*1024||!digest.matches("[a-f0-9]{32}"))throw new Failure(409,"Invalid track");
        if(part.length()>expected&&!part.delete())throw new IOException("Cannot reset partial file");
        if(part.length()==expected){
            if(verify(part,expected,digest)){control.progress(expected);return;}
            if(!part.delete())throw new IOException("Cannot reset partial file");
        }
        long offset=part.length();
        HttpURLConnection connection=control.open(url);
        connection.setConnectTimeout(15000);connection.setReadTimeout(60000);connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Cookie",cookie);connection.setRequestProperty("Accept-Encoding","identity");
        if(offset>0){connection.setRequestProperty("Range","bytes="+offset+"-");connection.setRequestProperty("If-Range","\""+digest+"\"");}
        control.connection(connection);
        try{
            check(control);int code=connection.getResponseCode();
            if(code!=200&&code!=206)throw new Failure(code,"HTTP "+code);
            String etag=connection.getHeaderField("ETag");
            if(etag!=null&&!etag.equals("\""+digest+"\""))throw new Failure(409,"File changed");
            long remaining=expected-offset;
            if(code==206){
                Matcher range=Pattern.compile("bytes ([0-9]+)-([0-9]+)/([0-9]+)").matcher(String.valueOf(connection.getHeaderField("Content-Range")));
                if(!range.matches()||Long.parseLong(range.group(1))!=offset||Long.parseLong(range.group(2))!=expected-1||Long.parseLong(range.group(3))!=expected)
                    throw new Failure(409,"Invalid byte range");
            }else{offset=0;remaining=expected;}
            long content=connection.getContentLengthLong();
            if(content>=0&&content!=remaining)throw new Failure(409,"Invalid response size");
            check(control);
            try(InputStream input=connection.getInputStream();RandomAccessFile output=new RandomAccessFile(part,"rw")){
                if(offset==0)output.setLength(0);output.seek(offset);byte[] buffer=new byte[65536];long received=offset;
                control.progress(received);
                while(received<expected){
                    check(control);int n=input.read(buffer,0,(int)Math.min(buffer.length,expected-received));
                    if(n<0)throw new EOFException("Incomplete download");
                    if(n==0)continue;output.write(buffer,0,n);received+=n;control.progress(received);
                }
                output.getFD().sync();
            }
            check(control);
            if(!verify(part,expected,digest)){part.delete();throw new Failure(422,"Checksum mismatch");}
        }catch(NumberFormatException invalid){throw new Failure(409,"Invalid byte range");}
        finally{control.connection(null);connection.disconnect();}
    }
}
