package com.aidj.music;
import javax.crypto.Cipher;
import java.util.Base64;
public class TransferCryptoProbe {
    public static void main(String[] args)throws Exception{
        TransferCrypto crypto=new TransferCrypto("test-phone-import-xxxxxxxxxxxxxxxxxxxxxxxxx");
        byte[] result=crypto.cipher("encrypt".equals(args[0])?Cipher.ENCRYPT_MODE:Cipher.DECRYPT_MODE,TransferCrypto.decode(args[1]),args[2]).doFinal(Base64.getDecoder().decode(args[3]));
        System.out.println(Base64.getEncoder().encodeToString(result));
    }
}
