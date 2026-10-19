/** Static export: no paid SSR/functions, image optimizer or runtime server. */
const config = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  experimental: { cpus: 2 },
};
export default config;
