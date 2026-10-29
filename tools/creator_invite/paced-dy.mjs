// tools/creator_invite/paced-dy.mjs — runs douyin-engineering's command line (what `node bin/dy.mjs …` runs) unchanged, except that every platform request
// of its page transport waits until at least CREATOR_INVITE_GAP_MS after the previous one (also across processes: CREATOR_INVITE_STAMP holds the time of
// the last request), and the requests of one process go one at a time. CREATOR_INVITE_REQUEST_LOG (optional) gets one JSON line per request: the time,
// the method and the path — no query, no body, no answer. Nothing in the CLI's folder is changed. Used by tools/creator_invite/invite.py.
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = process.env.CREATOR_INVITE_CLI_ROOT;
const gap = Number(process.env.CREATOR_INVITE_GAP_MS ?? '3000');
const stamp = process.env.CREATOR_INVITE_STAMP;
const log = process.env.CREATOR_INVITE_REQUEST_LOG;
if (!root || !stamp || !Number.isFinite(gap) || gap < 0) {
    process.stderr.write('[USAGE] paced-dy.mjs needs CREATOR_INVITE_CLI_ROOT, CREATOR_INVITE_STAMP and a CREATOR_INVITE_GAP_MS of 0 or more\n');
    process.exit(2);
}
const lastAt = () => { try { return Number(readFileSync(stamp, 'utf8')) || 0; } catch { return 0; } };
const url = (p) => pathToFileURL(path.join(root, p)).href;

const { PageTransport } = await import(url('src/transport.mjs'));
if (typeof PageTransport?.prototype?.request !== 'function') {
    process.stderr.write('[USAGE] paced-dy.mjs: this douyin-engineering has no PageTransport.prototype.request to pace; nothing was sent\n');
    process.exit(2);
}
const original = PageTransport.prototype.request;
let chain = Promise.resolve();
PageTransport.prototype.request = function paced(req, opts) {
    const self = this;
    const run = chain.then(async () => {
        const wait = lastAt() + gap - Date.now();
        if (wait > 0) await new Promise((resolve) => { setTimeout(resolve, wait); });
        const at = new Date().toISOString();
        try {
            return await original.call(self, req, opts);
        } finally {
            try { writeFileSync(stamp, String(Date.now()), { mode: 0o600 }); } catch { /* the next request waits from an older time: never shorter than the gap of this process */ }
            if (log) {
                let where = '';
                try { where = new URL(req.url).pathname; } catch { where = '?'; }
                try { appendFileSync(log, `${JSON.stringify({ at, cli: 'dy', method: req.method, path: where })}\n`, { mode: 0o600 }); } catch { /* counting never fails a request */ }
            }
        }
    });
    chain = run.catch(() => {});
    return run;
};

const { main } = await import(url('src/cli.mjs'));
process.exitCode = await main(process.argv.slice(2));
