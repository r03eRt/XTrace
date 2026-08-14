export default function HomePage() {
  return (
    <main
      data-testid="home"
      style={{
        display: "flex",
        minHeight: "100vh",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "0.5rem",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <h1 data-testid="home-title">Proyect-skeleton</h1>
      <p>Base Spec-Driven multiagente lista. Empieza por AGENTS.md y docs/USAGE.md.</p>
    </main>
  );
}
