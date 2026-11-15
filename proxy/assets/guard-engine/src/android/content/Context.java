package android.content;

import fnmedia.guard.MemoryPreferences;
import java.io.File;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** Small host Context for converted Spider code, not an Android framework implementation. */
public class Context {
    public static final int MODE_PRIVATE = 0;
    public static final int MODE_APPEND = 32768;
    public static final int PERMISSION_GRANTED = 0;
    public static final String WINDOW_SERVICE = "window";
    public static final String INPUT_METHOD_SERVICE = "input_method";
    public static final String CONNECTIVITY_SERVICE = "connectivity";
    public static final String WIFI_SERVICE = "wifi";
    public static final String CLIPBOARD_SERVICE = "clipboard";
    public static final String TELEPHONY_SERVICE = "phone";

    private final File root;
    private final ClassLoader loader;
    private final Map<String, SharedPreferences> preferences = new ConcurrentHashMap<>();

    public Context(File root, ClassLoader loader) {
        this.root = root;
        this.loader = loader;
        root.mkdirs();
    }
    protected Context(Context base) { this(base.root, base.loader); }
    private File directory(String name) { File dir = new File(root, name); dir.mkdirs(); return dir; }
    public File getCacheDir() { return directory("cache"); }
    public File getCodeCacheDir() { return directory("code_cache"); }
    public File getFilesDir() { return directory("files"); }
    public File getNoBackupFilesDir() { return directory("no_backup"); }
    public File getExternalCacheDir() { return getCacheDir(); }
    public File getExternalFilesDir(String type) { return directory("external/" + String.valueOf(type)); }
    public File getDatabasePath(String name) { return new File(directory("databases"), name); }
    public File getDir(String name, int mode) { return directory("app_" + name); }
    public String getPackageName() { return "com.fongmi.android.tv"; }
    public ClassLoader getClassLoader() { return loader; }
    public SharedPreferences getSharedPreferences(String name, int mode) {
        return preferences.computeIfAbsent(name, key -> new MemoryPreferences());
    }
    public Context getApplicationContext() { return this; }
    public Object getSystemService(String name) { return null; }
    public <T> T getSystemService(Class<T> type) { return null; }
    public String getSystemServiceName(Class<?> type) { return null; }
    public android.content.res.Resources getResources() { return null; }
    public android.content.res.Resources.Theme getTheme() { return null; }
    public void setTheme(int id) { }
    public String getString(int id) { return String.valueOf(id); }
    public String getString(int id, Object... args) { return String.valueOf(id); }
    public CharSequence getText(int id) { return String.valueOf(id); }
    public int getColor(int id) { return 0; }
    public android.graphics.drawable.Drawable getDrawable(int id) { return null; }
    public android.content.res.ColorStateList getColorStateList(int id) { return null; }
    public android.os.Looper getMainLooper() { return android.os.Looper.getMainLooper(); }
    public void startActivity(Intent intent) { }
    public void startActivity(Intent intent, android.os.Bundle options) { }
    public int checkCallingOrSelfPermission(String permission) { return PERMISSION_GRANTED; }
    public int checkSelfPermission(String permission) { return PERMISSION_GRANTED; }
    public int checkPermission(String permission, int pid, int uid) { return PERMISSION_GRANTED; }
    public android.content.pm.PackageManager getPackageManager() { return null; }
    public ContentResolver getContentResolver() { return null; }
    public android.content.res.AssetManager getAssets() { return null; }
    public android.content.pm.ApplicationInfo getApplicationInfo() { return new android.content.pm.ApplicationInfo(); }
    public String getPackageCodePath() { return ""; }
    public String getPackageResourcePath() { return ""; }
}
