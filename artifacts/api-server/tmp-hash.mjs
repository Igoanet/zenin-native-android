import { hashPassword } from "./dist/index.mjs";
const password = process.argv[2] || "password123";
const { hash, salt } = await hashPassword(password);
console.log(JSON.stringify({ hash, salt, password }));
