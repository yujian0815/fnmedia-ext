package fnmedia.guard;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/** Extracts only the FTY ARM64 library; other native families are deliberately rejected. */
final class GuardArchive {
    private GuardArchive() { }

    static Path arm64Library(Path archive, Path work) throws IOException {
        try (ZipFile zip = new ZipFile(archive.toFile())) {
            ZipEntry entry = zip.getEntry("assets/ftyguard_v8.so");
            if (entry == null) throw new IOException("Only assets/ftyguard_v8.so is supported");
            byte[] bytes;
            try (InputStream input = zip.getInputStream(entry)) {
                bytes = input.readNBytes(16 * 1024 * 1024 + 1);
            }
            if (bytes.length > 16 * 1024 * 1024) throw new IOException("Native library too large");
            if (bytes.length < 20 || bytes[0] != 0x7f || bytes[1] != 'E' || bytes[2] != 'L'
                    || bytes[3] != 'F' || bytes[4] != 2 || (bytes[18] & 0xff) != 183) {
                throw new IOException("Expected a little-endian ARM64 ELF");
            }
            Files.createDirectories(work);
            Path target = work.resolve("ftyguard_v8.so");
            Path temporary = Files.createTempFile(work, "native-", ".tmp");
            try {
                Files.write(temporary, bytes);
                Files.move(temporary, target, StandardCopyOption.REPLACE_EXISTING);
            } finally {
                Files.deleteIfExists(temporary);
            }
            return target;
        }
    }
}
