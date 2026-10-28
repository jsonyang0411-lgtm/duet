// tools/creator_invite/paced-jst.mjs — runs jushuitan-engineering's command line (what `node bin/jst.mjs …` runs) unchanged, except that it hands the CLI
// a fetch that waits until at least CREATOR_INVITE_GAP_MS after the previous platform request (also across processes: CREATOR_INVITE_STAMP) and sends
// one request at a time. CREATOR_INVITE_REQUEST_LOG (optional) gets one JSON line per request: the time, the method, the path and the HTTP status —
// never the token, the query or a body. Nothing in the CLI's folder is changed. Used by tools/creator_invite/invite.py.
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = process.env.CREATOR_INVITE_CLI_ROOT;
const gap = Number(process.env.CREATOR_INVITE_GAP_MS ?? '3000');
const stamp = process.env.CREATOR_INVITE_STAMP;
const log = process.env.CREATOR_INVITE_REQUEST_LOG;
if (!root || !stamp || !Number.isFinite(gap) || gap < 0) {
    process.stderr.write('[USAGE] paced-jst.mjs needs CREATOR_INVITE_CLI_ROOT, CREATOR_INVITE_STAMP and a CREATOR_INVITE_GAP_MS of 0 or more\n');
    process.exit(2);
}
const lastAt = () => { try { return Number(readFileSync(stamp, 'utf8')) || 0; } catch { return 0; } };

let chain = Promise.resolve();
const pacedFetch = (target, init) => {
    const run = chain.then(async () => {
        const wait = lastAt() + gap - Date.now();
        if (wait > 0) await new Promise((resolve) => { setTimeout(resolve, wait); });
        const at = new Date().toISOString();
        let res = null;
        try {
            res = await fetch(target, init);
            return res;
        } finally {
            try { writeFileSync(stamp, String(Date.now()), { mode: 0o600 }); } catch { /* see paced-dy.mjs */ }
            if (log) {
                let where = '';
                try { where = new URL(String(target)).pathname; } catch { where = '?'; }
                try { appendFileSync(log, `${JSON.stringify({ at, cli: 'jst', method: init?.method ?? 'GET', path: where, status: res?.status ?? null })}\n`, { mode: 0o600 }); } catch { /* counting never fails a request */ }
            }
        }
    });
    chain = run.catch(() => {});
    return run;
};

const { main } = await import(pathToFileURL(path.join(root, 'src/cli.mjs')).href);
process.exitCode = await main(process.argv.slice(2), { fetchImpl: pacedFetch });
