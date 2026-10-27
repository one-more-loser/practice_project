// Only invoked by the gated run launcher. No old Vite config, .env or application secrets.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const run = 'INT-20261026-01';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const privateDir = path.join(root, '.runtime', run);
try {
  const args = process.argv.slice(2);
  const arg = name => args[args.indexOf(name) + 1];
  if (args.length !== 6 || ['--run', '--enter-real-stage', '--source-digest'].some(name => args.filter(value => value === name).length !== 1) ||
      arg('--run') !== run || arg('--enter-real-stage') !== run || !/^[a-f0-9]{64}$/.test(arg('--source-digest'))) throw Error('command_scope_mismatch');
  if (Object.keys(process.env).some(key => /^(CS_|OPS_|VITE_)/.test(key) || key === 'NODE_OPTIONS')) throw Error('inherited_environment');
  const config = JSON.parse(await readFile(path.join(privateDir, 'frontend-public.json'), 'utf8'));
  if (config.run_id !== run || config.models !== 'disabled' || config.api_host !== '127.0.0.1' ||
      config.api_port !== 18120 || config.frontend_port !== 18174 || config.browser_origin !== 'http://127.0.0.1:18173' ||
      config.loaded_source_digest !== arg('--source-digest')) throw Error('config_scope_mismatch');
  const frozen = JSON.parse(await readFile(path.join(privateDir, 'source-freeze.json'), 'utf8'));
  const actual = {};
  for (const relative of Object.keys(frozen.files).sort()) {
    const source = path.resolve(root, relative);
    if (!source.startsWith(root + path.sep)) throw Error('source_path_invalid');
    actual[relative] = createHash('sha256').update(await readFile(source)).digest('hex');
    if (actual[relative] !== frozen.files[relative]) throw Error('source_changed');
  }
  if (createHash('sha256').update(JSON.stringify(actual)).digest('hex') !== config.loaded_source_digest) throw Error('source_digest_mismatch');
  const { createServer } = await import(pathToFileURL(path.join(root, 'frontend/node_modules/vite/dist/node/index.js')).href);
  const { default: vue } = await import(pathToFileURL(path.join(root, 'frontend/node_modules/@vitejs/plugin-vue/dist/index.mjs')).href);
  const server = await createServer({ configFile: false, envFile: false, root: path.join(root, 'frontend'),
    base: '/operations/', cacheDir: path.join(privateDir, 'vite-cache'), clearScreen: false, logLevel: 'silent',
    plugins: [vue(), { name: 'int-run-health', configureServer(s) { s.middlewares.use((req, res, next) => {
      if (req.url === '/__int_health' || req.url === '/operations/__int_health') {
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ run_id: run, service: 'ops02-web', pid: process.pid, port: 18174, models: 'disabled', loaded_source_digest: config.loaded_source_digest }));
      } else next();
    }); } }],
    server: { host: '127.0.0.1', port: 18174, strictPort: true,
      proxy: { '/api/ops/v1': { target: 'http://127.0.0.1:18120', changeOrigin: false },
        '/api/v1': { target: 'http://127.0.0.1:18080', changeOrigin: false },
        '/login': { target: 'http://127.0.0.1:18080', changeOrigin: false } } }
  });
  await server.listen();
  for (const event of ['SIGINT', 'SIGTERM']) process.on(event, async () => { await server.close(); process.exit(0); });
} catch {
  console.error(JSON.stringify({ run_id: run, status: 'refused', reason: 'frontend_configuration_or_start_failed' }));
  process.exitCode = 2;
}
