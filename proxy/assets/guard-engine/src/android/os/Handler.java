package android.os;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

public class Handler {
    private static final ScheduledExecutorService TASKS = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread thread = new Thread(r, "guard-handler");
        thread.setDaemon(true);
        return thread;
    });
    public Handler() { }
    public Handler(Looper looper) { }
    public boolean post(Runnable task) { TASKS.execute(task); return true; }
    public boolean postDelayed(Runnable task, long delayMillis) { TASKS.schedule(task, delayMillis, TimeUnit.MILLISECONDS); return true; }
    public void removeCallbacks(Runnable task) { }
    public void removeCallbacksAndMessages(Object token) { }
    public Looper getLooper() { return Looper.getMainLooper(); }
}
