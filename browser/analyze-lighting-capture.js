// Decode completed Flydigi A4/LGHT writes from a hid-sniffer JSON export.
// Usage: node analyze-lighting-capture.js capture.json [...]
const fs = require('fs');

for (const filename of process.argv.slice(2)) {
  const capture = JSON.parse(fs.readFileSync(filename, 'utf8'));
  let transaction = null;
  let count = 0;
  console.log(`\n${filename}`);

  for (const frame of capture.frames || []) {
    if (frame.direction !== 'OUT' || frame.bytes?.[2] !== 0xa4) continue;
    const payloadLength = frame.bytes[3] - 2;
    const payload = frame.bytes.slice(4, 4 + payloadLength);
    const stage = payload[0];

    if (stage === 0) {
      transaction = {
        ram: payload[1],
        wrappedLength: payload[2] | (payload[3] << 8),
        bytes: [],
      };
    } else if (stage === 1 && transaction) {
      const offset = payload[1] | (payload[2] << 8);
      const chunkLength = payload[3];
      for (let index = 0; index < chunkLength; index += 1) {
        transaction.bytes[offset + index] = payload[4 + index];
      }
    } else if (stage === 2 && transaction) {
      const bytes = Buffer.from(transaction.bytes);
      const header = bytes.indexOf('LGHT');
      if (header >= 0) {
        const dataLength = bytes.readUInt32LE(header + 6);
        const data = bytes.subarray(header + 12, header + 12 + dataLength);
        count += 1;
        console.log(
          `${String(count).padStart(2)} RAM=${transaction.ram} wrapped=${transaction.wrappedLength} ` +
          `payload=${data.length}: ${data.toString('hex').match(/../g)?.join(' ') || ''}`,
        );
      }
      transaction = null;
    }
  }
}
