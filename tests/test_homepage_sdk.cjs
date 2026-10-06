// Requires Docker and yq. Exercise the real init image, lockfile and restrictions.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'homepage-sdk-'));
const yaml = (file, expression) => JSON.parse(execFileSync('yq', ['-o=json', expression, file], { encoding: 'utf8' }));
const deployment = yaml('apps/config/homepage/metrics-deployment.yaml', 'select(.kind == "Deployment")');
const spec = deployment.spec.template.spec;
const init = spec.initContainers[0];
const runtime = spec.containers[0];
const config = yaml('apps/config/homepage/metrics-configmap.yaml', '.data');
try {
  for (const dir of ['config', 'work', 'tmp', 'data']) {
    fs.mkdirSync(path.join(temp, dir));
    fs.chmodSync(path.join(temp, dir), 0o777);
  }
  // Parent must be traversable by the pod's non-root UID.
  fs.chmodSync(temp, 0o755);
  for (const [name, content] of Object.entries(config)) {
    fs.writeFileSync(path.join(temp, 'config', name), content);
  }
  const common = ['run', '--rm', '--read-only', '--cap-drop=ALL',
    '--security-opt=no-new-privileges', '--user',
    `${spec.securityContext.runAsUser}:${spec.securityContext.runAsGroup}`,
    '--workdir', '/work', '-v', `${temp}/tmp:/tmp`];
  // Convert Kubernetes Mi/Gi quantities to Docker's m/g notation.
  const memory = container => container.resources.limits.memory.replace('Mi', 'm').replace('Gi', 'g');
  execFileSync('docker', [...common, '--memory', memory(init),
    '-v', `${temp}/config:/config:ro`, '-v', `${temp}/work:/work`,
    ...(init.env || []).flatMap(({ name, value }) => ['-e', `${name}=${value}`]),
    init.image, ...init.command, ...init.args], { stdio: 'inherit' });
  // Loading the module alone doesn't load its native addon: open and query a DB.
  const smoke = `
    const Database = require('better-sqlite3');
    const db = new Database(':memory:');
    if (db.prepare('SELECT 42 AS answer').get().answer !== 42) process.exit(1);
    db.close();
    const api = require('@actual-app/api');
    api.init({dataDir: '/data'}).then(() => api.shutdown()).then(() => {
      console.log('SQLite query and Actual SDK init/shutdown passed in slim runtime');
      process.exit(0);
    }).catch(error => { console.error(error); process.exit(1); });
  `;
  execFileSync('docker', [...common, '--memory', memory(runtime),
    '-v', `${temp}/work:/work:ro`, '-v', `${temp}/data:/data`,
    runtime.image, 'node', '-e', smoke], { stdio: 'inherit' });
} finally {
  // CI's host UID may differ from the container UID that owns build files.
  try { fs.rmSync(temp, { recursive: true, force: true }); }
  catch (error) {
    if (!['EACCES', 'EPERM'].includes(error.code)) throw error;
    console.warn(`Container-owned test files remain in ephemeral runner directory: ${temp}`);
  }
}
