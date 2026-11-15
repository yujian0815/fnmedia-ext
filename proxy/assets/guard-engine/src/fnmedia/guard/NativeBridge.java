package fnmedia.guard;

import com.github.unidbg.AndroidEmulator;
import com.github.unidbg.linux.android.dvm.DvmClass;
import com.github.unidbg.linux.android.dvm.DvmObject;
import com.github.unidbg.linux.android.dvm.VM;
import com.github.unidbg.linux.android.dvm.jni.ProxyDvmObject;

/** Process-local JNI transport. A single engine process owns a single emulator session. */
public final class NativeBridge {
    private static AndroidEmulator emulator;
    private static VM vm;
    private static DvmClass owner;

    private NativeBridge() { }

    static synchronized void bind(AndroidEmulator android, VM dalvik, DvmClass nativeOwner) {
        emulator = android;
        vm = dalvik;
        owner = nativeOwner;
    }

    public static synchronized Object invoke(String signature, Object... values) {
        if (emulator == null || vm == null || owner == null) {
            throw new IllegalStateException("Guard native session is not initialized");
        }
        Object[] guest = new Object[values.length];
        for (int i = 0; i < values.length; i++) guest[i] = ProxyDvmObject.createObject(vm, values[i]);
        return unwrap(owner.callStaticJniMethodObject(emulator, signature, guest));
    }

    private static Object unwrap(DvmObject<?> object) {
        if (object == null) return null;
        Object value = object.getValue();
        if (value instanceof DvmObject<?>[] array) {
            Object[] result = new Object[array.length];
            for (int i = 0; i < array.length; i++) result[i] = unwrap(array[i]);
            return result;
        }
        return value;
    }
}
