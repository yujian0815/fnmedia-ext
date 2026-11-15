package android.content;

import java.io.File;

public class ContextWrapper extends Context {
    private final Context base;
    public ContextWrapper(Context base) { super(base); this.base = base; }
    public ContextWrapper(File root, ClassLoader loader) { super(root, loader); base = null; }
    public Context getBaseContext() { return base == null ? this : base; }
    @Override public Context getApplicationContext() { return base == null ? this : base.getApplicationContext(); }
}
