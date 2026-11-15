package fnmedia.guard;

import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Executors;

/** Minimal loopback proxy contract used by ProxyOrigin; no Android UI or cloud login service. */
public final class LocalProxy {
    public interface Handler { Object[] invoke(Map<String, String> parameters) throws Exception; }
    private static volatile Handler handler;
    private static HttpServer server;
    private static String base;

    private LocalProxy() { }

    public static void setHandler(Handler value) { handler = value; }

    public static synchronized String getUrl() {
        start();
        return base + "media";
    }

    public static synchronized String getOwnProxyUrl() { return getOwnProxyUrl(""); }

    public static synchronized String getOwnProxyUrl(String name) {
        start();
        return base + "drive/" + (name == null || name.isEmpty() ? "" : "proxy/" + name + "/");
    }

    public static String getProxyUrlDo(String action) { return getUrl() + "?do=" + action; }

    private static void start() {
        if (server != null) return;
        try {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 8);
            server.setExecutor(Executors.newSingleThreadExecutor(r -> {
                Thread thread = new Thread(r, "guard-local-proxy");
                thread.setDaemon(true);
                return thread;
            }));
            server.createContext("/", exchange -> {
                try {
                    Map<String, String> params = query(exchange.getRequestURI().getRawQuery());
                    Handler current = handler;
                    if (current == null) {
                        respond(exchange, 503, "text/plain", "Guard proxy is not ready".getBytes(StandardCharsets.UTF_8));
                        return;
                    }
                    Object[] result = current.invoke(params);
                    int status = result != null && result.length > 0 && result[0] instanceof Number n ? n.intValue() : 200;
                    String type = result != null && result.length > 1 ? String.valueOf(result[1]) : "application/octet-stream";
                    byte[] body = result != null && result.length > 2 && result[2] instanceof byte[] b ? b : new byte[0];
                    respond(exchange, status, type, body);
                } catch (Exception error) {
                    respond(exchange, 500, "text/plain", String.valueOf(error).getBytes(StandardCharsets.UTF_8));
                }
            });
            server.start();
            base = "http://127.0.0.1:" + server.getAddress().getPort() + "/" + UUID.randomUUID() + "/";
        } catch (IOException error) {
            throw new IllegalStateException("Cannot start Guard local proxy", error);
        }
    }

    private static void respond(com.sun.net.httpserver.HttpExchange exchange, int status,
            String type, byte[] body) throws IOException {
        exchange.getResponseHeaders().set("Content-Type", type);
        exchange.sendResponseHeaders(status, body.length);
        exchange.getResponseBody().write(body);
        exchange.close();
    }

    private static Map<String, String> query(String raw) {
        Map<String, String> result = new HashMap<>();
        if (raw == null) return result;
        for (String pair : raw.split("&")) {
            int split = pair.indexOf('=');
            String key = split < 0 ? pair : pair.substring(0, split);
            String value = split < 0 ? "" : pair.substring(split + 1);
            result.put(URLDecoder.decode(key, StandardCharsets.UTF_8),
                    URLDecoder.decode(value, StandardCharsets.UTF_8));
        }
        return result;
    }
}
