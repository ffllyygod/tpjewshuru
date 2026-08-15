import { defineRailway, image, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const postgresData = volume("postgres-data");

  const postgres = service("postgres", {
    source: image("pgvector/pgvector:pg16"),
    replicas: { "ams": 1 },
    networking: { tcpProxies: { "5432": {} } },
    env: {
      POSTGRES_DB: preserve(),
      POSTGRES_PASSWORD: preserve(),
      POSTGRES_USER: preserve(),
    },
    volumeMounts: {
      "/var/lib/postgresql/data": postgresData,
    },
  });
  const api = service("api", {
    replicas: { "ams": 1 },
    env: {
      DATABASE_URL: preserve(),
      LLM_API_KEY: preserve(),
      LLM_BASE_URL: preserve(),
      LLM_MODEL: preserve(),
    },
  });

  return project("tpjewellers-chatbot", {
    resources: [postgres, api],
  });
});
