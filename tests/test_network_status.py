from tests.test_native_downloads import compile_probe


def test_native_reachability_uses_request_evidence_and_ignores_old_failures(tmp_path):
    fixtures={'com/aidj/music/NetworkProbe.java':'''package com.aidj.music;
public class NetworkProbe {
 static void check(boolean v,String s){if(!v)throw new AssertionError(s);}
 public static void main(String[] args)throws Exception{
  check(!NetworkStatus.snapshot().getBoolean("known"),"unknown is not confirmed offline");
  long slow=NetworkStatus.begin(),fast=NetworkStatus.begin();NetworkStatus.finish(fast,true);NetworkStatus.finish(slow,false);
  check(NetworkStatus.snapshot().getBoolean("reachable"),"late failed request cannot erase a newer successful request");
  NetworkStatus.finish(NetworkStatus.begin(),false);check(!NetworkStatus.snapshot().getBoolean("reachable"),"real failure is reported");
  NetworkStatus.finish(NetworkStatus.begin(),true);check(NetworkStatus.snapshot().getBoolean("reachable"),"background success recovers without browser network event");
  check(DownloadPolicy.cause(new java.net.UnknownHostException()).equals("dns"),"dns");
  check(DownloadPolicy.cause(new java.net.SocketTimeoutException()).equals("timeout"),"timeout");
  check(DownloadPolicy.cause(new javax.net.ssl.SSLHandshakeException("private details")).equals("tls"),"tls is categorical, never raw exception");
  check(DownloadPolicy.cause(new java.io.EOFException()).equals("incomplete"),"partial stream");
  System.out.println("PROBE_PASSED native_recovery stale_results classified_errors");
 }
}'''}
    compile_probe(tmp_path,fixtures,['NetworkStatus.java','DownloadPolicy.java','DownloadTransfer.java'],'NetworkProbe')
