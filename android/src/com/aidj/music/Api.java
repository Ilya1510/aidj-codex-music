package com.aidj.music;

import android.content.Context;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;

final class Api {
    static String base(Context c){return State.get(c).read("server",BuildConfig.SERVER);}
    static String cookie(Context c){return State.get(c).read("session","");}
    static String validate(String value)throws Exception{
        URI u=new URI(value);if(!"https".equals(u.getScheme())||u.getHost()==null||u.getUserInfo()!=null||u.getPort()!=-1||!(u.getPath()==null||u.getPath().isEmpty()||"/".equals(u.getPath()))||u.getQuery()!=null||u.getFragment()!=null)throw new IOException("Неверный адрес сервера.");
        return "https://"+u.getHost();
    }
    static JSONObject request(Context c,String path,JSONObject body)throws Exception{return requestTo(c,base(c),path,body,false);}
    static JSONObject requestTo(Context c,String server,String path,JSONObject body,boolean connecting)throws Exception{
        if(!path.matches("/api/(catalog|dj|edit|range|reviews|session|auth|android-version|browser-link|import-register|device-inbox|device-ack|library-version|discover(?:/[1-9][0-9]{0,12})?)"))throw new IOException("Недопустимый запрос.");
        long request=NetworkStatus.begin();boolean reached=false;
        HttpURLConnection connection=(HttpURLConnection)new URL(validate(server)+path).openConnection();
        connection.setConnectTimeout(15000);connection.setReadTimeout(60000);connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Accept","application/json");connection.setRequestProperty("X-AIDJ-App","1");
        connection.setRequestProperty("X-AIDJ-Version",String.valueOf(BuildConfig.VERSION_CODE));
        String requestSession=cookie(c);
        if(!connecting&&!requestSession.isEmpty())connection.setRequestProperty("Cookie",requestSession);
        try{
            if(body!=null){connection.setRequestMethod("POST");connection.setDoOutput(true);connection.setRequestProperty("Content-Type","application/json");byte[] data=body.toString().getBytes(StandardCharsets.UTF_8);connection.setFixedLengthStreamingMode(data.length);try(OutputStream out=connection.getOutputStream()){out.write(data);}}
            int code=connection.getResponseCode();reached=true;InputStream stream=code>=400?connection.getErrorStream():connection.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream();
            int maxResponse=("/api/catalog".equals(path)||"/api/edit".equals(path)?8:2)*1024*1024;
            if(stream!=null)try(InputStream in=stream){byte[] buffer=new byte[16384];int n;while((n=in.read(buffer))!=-1){out.write(buffer,0,n);if(out.size()>maxResponse)throw new IOException("Слишком большой ответ.");}}
            String text=out.toString("UTF-8");JSONObject result=new JSONObject();result.put("status",code);try{result.put("body",text.isEmpty()?new JSONObject():new JSONObject(text));}
            catch(JSONException invalid){result.put("body",new JSONObject().put("error","Сервер временно недоступен. Попробуй ещё раз."));if(code<400)result.put("status",502);}
            if(!connecting&&(!requestSession.equals(cookie(c))||!validate(server).equals(base(c))))return new JSONObject().put("status",409).put("body",new JSONObject().put("error","Вход в приложение изменился. Обнови библиотеку."));
            if(connecting&&code==200){String header=connection.getHeaderField("Set-Cookie");String session=header==null?"":header.split(";",2)[0];if(!session.matches("aidj_session=[A-Za-z0-9_-]{32,100}"))throw new IOException("Не удалось сохранить вход.");State.get(c).write("server",validate(server));State.get(c).write("session",session);}
            JSONObject access=result.optJSONObject("body")==null?null:result.optJSONObject("body").optJSONObject("access");
            if(code==200&&access!=null&&("admin".equals(access.optString("role"))||"listener".equals(access.optString("role")))){
                if(connecting)State.get(c).write("web_catalog","null");
                State.get(c).write("access",access.toString());
            }
            return result;
        }finally{NetworkStatus.finish(request,reached);connection.disconnect();}
    }
}
