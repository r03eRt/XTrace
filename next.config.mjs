/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // La app debe fallar de forma explícita si faltan variables obligatorias.
  // Valida las env vars en src/lib/env.ts e impórtalo temprano.
};

export default nextConfig;
