// Encrypt a plaintext payload using cryptico.js (same as Flutterwave browser)
// Usage: node encrypt.js <plaintext_json> <public_key_b64>
// Output: the encrypted string (RSA+AES)

const fs = require('fs');
const path = require('path');

// Download and eval cryptico if not cached
const CRYPTICO_URL = 'https://cdnjs.cloudflare.com/ajax/libs/cryptico/0.0.1343522940/cryptico.min.js';
const CACHE_PATH = path.join(__dirname, '.cryptico_cache.js');

async function loadCryptico() {
    let code;
    if (fs.existsSync(CACHE_PATH)) {
        code = fs.readFileSync(CACHE_PATH, 'utf8');
    } else {
        const https = require('https');
        code = await new Promise((resolve, reject) => {
            https.get(CRYPTICO_URL, (res) => {
                let body = '';
                res.on('data', (chunk) => body += chunk);
                res.on('end', () => resolve(body));
                res.on('error', reject);
            });
        });
        fs.writeFileSync(CACHE_PATH, code);
    }
    // cryptico.js sets window.cryptico — fake window for Node
    global.window = global;
    global.navigator = { appName: 'Node' };
    const vm = require('vm');
    vm.runInThisContext(code);
}

async function main() {
    await loadCryptico();

    const plaintext = process.argv[2];
    const publicKey = process.argv[3];

    if (!plaintext || !publicKey) {
        console.error('Usage: node encrypt.js <plaintext> <public_key_b64>');
        process.exit(1);
    }

    const result = cryptico.encrypt(plaintext, publicKey);
    if (result.status === 'success') {
        // Output just the cipher text
        process.stdout.write(result.cipher);
    } else {
        console.error('Encryption failed:', result.status);
        process.exit(1);
    }
}

main().catch(e => { console.error(e); process.exit(1); });
