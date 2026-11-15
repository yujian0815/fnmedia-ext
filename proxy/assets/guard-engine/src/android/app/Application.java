package android.app;

import android.content.ContextWrapper;
import java.io.File;

/** Application identity expected by converted Guard code, backed only by host Context. */
public class Application extends ContextWrapper {
    public Application() { super(new File(System.getProperty("java.io.tmpdir"), "fnmedia-guard"), Application.class.getClassLoader()); }
    public Application(File root, ClassLoader loader) { super(root, loader); }
    public void registerActivityLifecycleCallbacks(ActivityLifecycleCallbacks callback) { }
    public void unregisterActivityLifecycleCallbacks(ActivityLifecycleCallbacks callback) { }
    public interface ActivityLifecycleCallbacks {
        void onActivityCreated(Activity activity, android.os.Bundle state);
        void onActivityStarted(Activity activity);
        void onActivityResumed(Activity activity);
        void onActivityPaused(Activity activity);
        void onActivityStopped(Activity activity);
        void onActivitySaveInstanceState(Activity activity, android.os.Bundle state);
        void onActivityDestroyed(Activity activity);
    }
}
