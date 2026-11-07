package com.aidj.music;

final class DownloadPolicy {
    static long retryDelay(int attempts){return Math.min(300000L,2000L*(1L<<Math.min(8,Math.max(0,attempts-1))));}
    static boolean permanent(int code,int attempts){return code==404||code==410||code==409||(code==422&&attempts>=3);}
    static String error(int code){
        if(code==401||code==403)return "Обнови подключение приложения через бота.";
        if(code==404||code==410)return "Песня больше недоступна на Диске.";
        if(code==409)return "Файл изменился. Обнови библиотеку.";
        if(code==422)return "Не удалось проверить файл. Попробуй скачать его снова.";
        return "Повторю загрузку автоматически";
    }
}
