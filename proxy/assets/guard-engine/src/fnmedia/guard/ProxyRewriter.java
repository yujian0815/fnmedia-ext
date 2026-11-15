package fnmedia.guard;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/** Small, source-independent rewrite: only Guard's proxy URL natives are hosted by LocalProxy. */
final class ProxyRewriter {
    private ProxyRewriter() { }

    static void rewrite(Path input, Path output) throws IOException {
        Path temporary = Files.createTempFile(output.getParent(), "proxy-", ".jar");
        try {
            try (ZipFile zip = new ZipFile(input.toFile());
                 ZipOutputStream jar = new ZipOutputStream(Files.newOutputStream(temporary))) {
                var entries = zip.entries();
                while (entries.hasMoreElements()) {
                    ZipEntry entry = entries.nextElement();
                    byte[] bytes;
                    try (InputStream stream = zip.getInputStream(entry)) {
                        bytes = stream.readAllBytes();
                    }
                    if (entry.getName().equals("com/github/catvod/spider/ProxyOrigin.class")) {
                        bytes = rewriteProxy(bytes);
                    }
                    jar.putNextEntry(new ZipEntry(entry.getName()));
                    jar.write(bytes);
                    jar.closeEntry();
                }
            }
            Files.move(temporary, output, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Files.deleteIfExists(temporary);
        }
    }

    private static byte[] rewriteProxy(byte[] bytes) {
        ClassReader reader = new ClassReader(bytes);
        ClassWriter writer = new ClassWriter(reader, 0);
        reader.accept(new ClassVisitor(Opcodes.ASM9, writer) {
            @Override public MethodVisitor visitMethod(int access, String name, String descriptor,
                    String signature, String[] exceptions) {
                boolean getUrl = name.equals("getUrl") && descriptor.equals("()Ljava/lang/String;");
                boolean own = name.equals("getOwnProxyUrl")
                        && (descriptor.equals("()Ljava/lang/String;")
                            || descriptor.equals("(Ljava/lang/String;)Ljava/lang/String;"));
                MethodVisitor method = super.visitMethod(access, name, descriptor, signature, exceptions);
                if ((access & Opcodes.ACC_STATIC) == 0 || (!getUrl && !own)) return method;
                method.visitCode();
                if (own && descriptor.startsWith("(")) method.visitVarInsn(Opcodes.ALOAD, 0);
                method.visitMethodInsn(Opcodes.INVOKESTATIC, "fnmedia/guard/LocalProxy",
                        getUrl ? "getUrl" : "getOwnProxyUrl", descriptor, false);
                method.visitInsn(Opcodes.ARETURN);
                method.visitMaxs(1, 1);
                method.visitEnd();
                return null;
            }
        }, 0);
        return writer.toByteArray();
    }
}
