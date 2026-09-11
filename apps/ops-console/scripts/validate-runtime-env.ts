import { validateOpsRuntimeEnv } from "../lib/ops-session";

const result = validateOpsRuntimeEnv();

if (!result.ok) {
  console.error(result.error);
  process.exit(1);
}

console.log("ops console runtime env ok");
