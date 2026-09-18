/**
 * На проде Caddy сам разводит /api на бота, а остальное на кабинет, поэтому
 * переписывать ничего не нужно. В разработке кабинет живёт на 3000, бот на
 * 8000, и без этого правила cookie сессии не долетит: она привязана к домену.
 */
// Внимание: rewrites вычисляются во время сборки, а не запуска. Поэтому
// API_ORIGIN нужен и при npm run build, а значение по умолчанию всегда есть.
const apiOrigin = process.env.API_ORIGIN || "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Для контейнера: Next кладёт в .next/standalone минимальный сервер со
  // своими зависимостями, и в образ не нужно тащить весь node_modules.
  output: "standalone",
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
