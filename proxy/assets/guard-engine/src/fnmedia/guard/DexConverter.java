package fnmedia.guard;

import com.googlecode.d2j.dex.Dex2jar;
import com.googlecode.d2j.reader.MultiDexFileReader;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.jar.JarFile;

/** Converts the decrypted DEX once per content hash, then reuses the JVM archive. */
public final class DexConverter {
    private static final String FORMAT = "fnmedia-guard-v1";
    private DexConverter() { }

    public static Path convert(Path archive, Path cache) throws Exception {
        byte[] source = Files.readAllBytes(archive);
        String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(source));
        Files.createDirectories(cache);
        Path output = cache.resolve(hash + "." + FORMAT + ".jar");
        try (FileChannel channel = FileChannel.open(cache.resolve(hash + ".lock"),
                StandardOpenOption.CREATE, StandardOpenOption.WRITE);
             var ignored = channel.lock()) {
            if (valid(output)) return output;
            Path converted = Files.createTempFile(cache, "converted-", ".jar");
            Path rewritten = Files.createTempFile(cache, "rewritten-", ".jar");
            try {
                if (source.length >= 2 && source[0] == 'P' && source[1] == 'K' && isJvmArchive(archive)) {
                    Files.write(converted, source);
                } else {
                    Dex2jar.from(MultiDexFileReader.open(source)).skipDebug(true)
                            .computeFrames(true).to(converted);
                }
                ProxyRewriter.rewrite(converted, rewritten);
                if (!valid(rewritten)) throw new IOException("No JVM classes after DEX conversion");
                Files.move(rewritten, output, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                return output;
            } finally {
                Files.deleteIfExists(converted);
                Files.deleteIfExists(rewritten);
            }
        }
    }

    private static boolean isJvmArchive(Path path) {
        try (JarFile jar = new JarFile(path.toFile())) {
            boolean dex = jar.stream().anyMatch(e -> e.getName().matches("classes[0-9]*\\.dex"));
            boolean classes = jar.stream().anyMatch(e -> e.getName().endsWith(".class"));
            return !dex && classes;
        } catch (IOException error) {
            return false;
        }
    }

    private static boolean valid(Path path) {
        if (!Files.isRegularFile(path)) return false;
        try (JarFile jar = new JarFile(path.toFile())) {
            return jar.stream().anyMatch(e -> e.getName().endsWith(".class"));
        } catch (IOException error) {
            return false;
        }
    }
}
