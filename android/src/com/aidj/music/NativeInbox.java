package com.aidj.music;

import android.content.Context;
import org.json.*;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.*;

/** Persist requested downloads before acknowledging them; ready means verified bytes. */
final class NativeInbox {
    private static final AtomicBoolean syncing=new AtomicBoolean(false);
    private static final AtomicBoolean scheduled=new AtomicBoolean(false);
    private static final ExecutorService network=Executors.newSingleThreadExecutor();
    static boolean busy(){return syncing.get();}
    static void request(Context c){
        if(syncing.get()||!scheduled.compareAndSet(false,true))return;
        Context app=c.getApplicationContext();network.execute(()->{try{sync(app);}finally{scheduled.set(false);}});
    }
    private static void prioritize(Context c,State state){
        try{
            JSONObject q=new JSONObject(state.read("nativeQueue","{}"));if(!q.optBoolean("djEnabled"))return;
            JSONArray entries=q.optJSONArray("entries");if(entries==null)return;
            int at=-1;for(int i=0;i<entries.length();i++)if(q.optString("currentId").equals(entries.getJSONObject(i).optString("id"))){at=i;break;}
            if(at<0)return;JSONArray ids=new JSONArray();java.util.Set<String> seen=new java.util.LinkedHashSet<>();
            for(int i=at;i<Math.min(entries.length(),at+6);i++){String id=entries.getJSONObject(i).optString("trackId");if(id.matches("[a-f0-9]{24}")&&seen.add(id))ids.put(id);}
            String key=ids.toString();long now=System.currentTimeMillis(),last=0;try{last=Long.parseLong(state.read("djPriorityAt","0"));}catch(Exception ignored){}
            if(ids.length()==0||key.equals(state.read("djServerPriority",""))&&now-last<900000)return;
            JSONObject body=new JSONObject().put("trackIds",ids).put("nextTrackIds",ids);
            if(Api.request(c,"/api/dj",body).optInt("status")==200){state.write("djServerPriority",key);state.write("djPriorityAt",String.valueOf(now));}
        }catch(Exception ignored){}  // Preparation never blocks delivery acknowledgements.
    }
    static boolean sync(Context context){
        if(!syncing.compareAndSet(false,true))return true;
        try{
            Context c=context.getApplicationContext();State state=State.get(c);if(Api.cookie(c).isEmpty())return true;
            String device=state.read("deviceId","");if(device.isEmpty()){device=UUID.randomUUID().toString();state.write("deviceId",device);}
            JSONObject body=new JSONObject();body.put("deviceId",device);body.put("diagnostics",NativeDownloads.diagnostics(c));
            JSONObject response=Api.request(c,"/api/device-inbox",body);if(response.optInt("status")!=200)return false;
            JSONObject inbox=response.getJSONObject("body"),receipts=new JSONObject(state.read("deliveryReceipts","{}"));
            JSONArray items=inbox.getJSONArray("items"),tracks=new JSONArray(),acks=new JSONArray();
            for(int i=0;i<items.length();i++){
                JSONObject item=items.getJSONObject(i),track=item.getJSONObject("track");String id=item.getString("id");
                if(!id.matches("[a-f0-9]{24}"))throw new JSONException("invalid delivery");
                JSONObject receipt=new JSONObject();receipt.put("id",id);receipt.put("revision",item.getLong("revision"));receipt.put("md5",track.getString("md5"));
                if(state.file(track.getString("md5"))!=null){receipt.put("status","ready");acks.put(receipt);receipts.remove(id);}
                else{tracks.put(track);receipt.put("status","queued");receipts.put(id,receipt);}
            }
            if(tracks.length()>0)NativeDownloads.add(c,tracks);
            JSONArray keys=receipts.names();
            if(keys!=null)for(int i=0;i<keys.length();i++){
                String id=keys.getString(i);JSONObject receipt=receipts.getJSONObject(id);String md5=receipt.getString("md5");
                if(state.file(md5)!=null)receipt.put("status","ready");
                else if(state.download(md5)==null)receipt.put("status","cancelled");
                acks.put(receipt);
            }
            state.write("deliveryReceipts",receipts.toString());state.write("libraryRevision",inbox.getString("revision"));
            if(acks.length()>0){
                // Batches remain bounded even after a long offline interval.
                for(int start=0;start<acks.length();start+=100){
                    JSONArray batch=new JSONArray();for(int i=start;i<Math.min(start+100,acks.length());i++)batch.put(acks.getJSONObject(i));
                    JSONObject ack=new JSONObject();ack.put("deviceId",device);ack.put("items",batch);
                    if(Api.request(c,"/api/device-ack",ack).optInt("status")!=200)return false;
                    for(int i=0;i<batch.length();i++){JSONObject receipt=batch.getJSONObject(i);if(!"queued".equals(receipt.getString("status")))receipts.remove(receipt.getString("id"));}
                    state.write("deliveryReceipts",receipts.toString());
                }
            }
            prioritize(c,state);return true;
        }catch(Exception ignored){return false;}
        finally{syncing.set(false);}
    }
}
