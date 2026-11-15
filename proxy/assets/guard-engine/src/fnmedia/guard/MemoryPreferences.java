package fnmedia.guard;

import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/** Process-local preferences sufficient for source settings; no credential store is implied. */
public final class MemoryPreferences implements SharedPreferences {
    private final Map<String, Object> values = new HashMap<>();
    @Override public synchronized Map<String, ?> getAll() { return new HashMap<>(values); }
    @Override public synchronized String getString(String key, String fallback) { Object v = values.get(key); return v instanceof String ? (String) v : fallback; }
    @SuppressWarnings("unchecked") @Override public synchronized Set<String> getStringSet(String key, Set<String> fallback) { Object v = values.get(key); return v instanceof Set ? new HashSet<>((Set<String>) v) : fallback; }
    @Override public synchronized int getInt(String key, int fallback) { Object v = values.get(key); return v instanceof Number ? ((Number) v).intValue() : fallback; }
    @Override public synchronized long getLong(String key, long fallback) { Object v = values.get(key); return v instanceof Number ? ((Number) v).longValue() : fallback; }
    @Override public synchronized float getFloat(String key, float fallback) { Object v = values.get(key); return v instanceof Number ? ((Number) v).floatValue() : fallback; }
    @Override public synchronized boolean getBoolean(String key, boolean fallback) { Object v = values.get(key); return v instanceof Boolean ? (Boolean) v : fallback; }
    @Override public synchronized boolean contains(String key) { return values.containsKey(key); }
    @Override public Editor edit() { return new MemoryEditor(); }
    @Override public void registerOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener listener) { }
    @Override public void unregisterOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener listener) { }

    private final class MemoryEditor implements Editor {
        private final Map<String, Object> updates = new HashMap<>();
        private final Set<String> removals = new HashSet<>();
        private boolean clear;
        @Override public Editor putString(String key, String value) { updates.put(key, value); return this; }
        @Override public Editor putStringSet(String key, Set<String> value) { updates.put(key, value == null ? null : new HashSet<>(value)); return this; }
        @Override public Editor putInt(String key, int value) { updates.put(key, value); return this; }
        @Override public Editor putLong(String key, long value) { updates.put(key, value); return this; }
        @Override public Editor putFloat(String key, float value) { updates.put(key, value); return this; }
        @Override public Editor putBoolean(String key, boolean value) { updates.put(key, value); return this; }
        @Override public Editor remove(String key) { removals.add(key); return this; }
        @Override public Editor clear() { clear = true; return this; }
        @Override public boolean commit() { apply(); return true; }
        @Override public void apply() {
            synchronized (MemoryPreferences.this) {
                if (clear) values.clear();
                for (String key : removals) values.remove(key);
                values.putAll(updates);
            }
        }
    }
}
