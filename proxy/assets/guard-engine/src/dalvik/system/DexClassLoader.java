package dalvik.system;

import fnmedia.guard.DexConverter;
import java.io.File;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** JVM-backed implementation of the Android class used by FTY's native loader. */
public final class DexClassLoader extends URLClassLoader {
    public static volatile DexClassLoader current;
    private static final Map<String, Class<?>> GUEST = new ConcurrentHashMap<>();

    public DexClassLoader(String path, String optimizedDirectory,
            String librarySearchPath, ClassLoader parent) throws Exception {
        super(new URL[] {
            DexConverter.convert(Path.of(path), Path.of(optimizedDirectory, "fnmedia-converted"))
                    .toUri().toURL(),
            new File(path).toURI().toURL()
        }, parent);
        current = this;
    }

    public static Class<?> pluginClass(String name) {
        DexClassLoader loader = current;
        if (loader == null) return null;
        try {
            return Class.forName(name, false, loader);
        } catch (ClassNotFoundException | LinkageError missing) {
            return null;
        }
    }

    public static Class<?> guestClass(String name) {
        Class<?> cached = GUEST.get(name);
        return cached != null ? cached : pluginClass(name);
    }

    @Override protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
        if (name.startsWith("com.github.catvod.") && !name.startsWith("com.github.catvod.crawler.")) {
            synchronized (getClassLoadingLock(name)) {
                Class<?> loaded = findLoadedClass(name);
                if (loaded == null) {
                    try {
                        loaded = findClass(name);
                    } catch (ClassNotFoundException notOwned) {
                        // Parent stubs/dependencies remain available.
                    }
                }
                if (loaded != null) {
                    if (resolve) resolveClass(loaded);
                    GUEST.put(name, loaded);
                    return loaded;
                }
            }
        }
        return super.loadClass(name, resolve);
    }
}
