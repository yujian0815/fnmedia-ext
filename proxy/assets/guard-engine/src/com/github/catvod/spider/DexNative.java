package com.github.catvod.spider;

import fnmedia.guard.NativeBridge;

/** Static methods called by decrypted FTY bytecode; every call stays inside Unidbg. */
public final class DexNative {
    private DexNative() { }
    public static String encrypt(String value) { return (String) NativeBridge.invoke("encrypt(Ljava/lang/String;)Ljava/lang/String;", value); }
    public static String decrypt(String value) { return (String) NativeBridge.invoke("decrypt(Ljava/lang/String;)Ljava/lang/String;", value); }
    public static String native_ting_md5(String value) { return (String) NativeBridge.invoke("native_ting_md5(Ljava/lang/String;)Ljava/lang/String;", value); }
    public static String noxSign(String a, String b, String c) { return (String) NativeBridge.invoke("noxSign(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;", a, b, c); }
    public static Object getLoader(Object value) { return NativeBridge.invoke("getLoader(Ljava/lang/Object;)Ljava/lang/Object;", value); }
    public static Object getSpider(Object loader, String name) { return NativeBridge.invoke("getSpider(Ljava/lang/Object;Ljava/lang/String;)Ljava/lang/Object;", loader, name); }
    public static int[] calcResult(int[] value) { return (int[]) NativeBridge.invoke("calcResult([I)[I", value); }
    public static Object[] proxyInvoke(Object loader, Object params) { return (Object[]) NativeBridge.invoke("proxyInvoke(Ljava/lang/Object;Ljava/lang/Object;)[Ljava/lang/Object;", loader, params); }
}
