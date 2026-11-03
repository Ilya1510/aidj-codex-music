package com.aidj.music;

import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.Base64;
import javax.crypto.*;
import javax.crypto.spec.*;

final class TransferCrypto {
    private final SecretKeySpec key;
    TransferCrypto(String token)throws Exception{key=new SecretKeySpec(MessageDigest.getInstance("SHA-256").digest(token.getBytes(StandardCharsets.UTF_8)),"AES");}
    Cipher cipher(int mode,byte[] iv,String path)throws Exception{
        if(iv.length!=12)throw new GeneralSecurityException("invalid nonce");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(mode,key,new GCMParameterSpec(128,iv));cipher.updateAAD(path.getBytes(StandardCharsets.UTF_8));return cipher;
    }
    byte[] nonce(){byte[] bytes=new byte[12];new SecureRandom().nextBytes(bytes);return bytes;}
    static String encode(byte[] value){return Base64.getUrlEncoder().withoutPadding().encodeToString(value);}
    static byte[] decode(String value){return Base64.getUrlDecoder().decode(value);}
}
