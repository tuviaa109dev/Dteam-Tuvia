import { useState } from "react";
import HealthPanel from "./components/HealthPanel.jsx";
import SubmitJobForm from "./components/SubmitJobForm.jsx";
import JobsTable from "./components/JobsTable.jsx";
import JobDetail from "./components/JobDetail.jsx";
import DevPanel from "./components/DevPanel.jsx";

export default function App() {
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [statusFilter, setStatusFilter] = useState(""); // shared by the stat tiles and the table dropdown
  const bump = () => setRefreshKey((k) => k + 1);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Job Queue</h1>
        <span className="muted">API → Redis queue → workers → PostgreSQL</span>
      </header>

      <HealthPanel refreshKey={refreshKey} activeStatus={statusFilter} onSelectStatus={setStatusFilter} />

      <main className="layout">
        <div className="side">
          <section className="card">
            <h2>Submit a job</h2>
            <SubmitJobForm
              onSubmitted={(job) => {
                setSelectedId(job.id);
                bump();
              }}
            />
          </section>
        </div>

        <div className="side grow">
          <section className="card">
            <JobsTable
              refreshKey={refreshKey}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onChanged={bump}
              status={statusFilter}
              onStatusChange={setStatusFilter}
            />
          </section>
          {/* Dev tools are tucked away: they only appear while the Processing stat is selected. */}
          {statusFilter === "processing" && (
            <DevPanel
              onFilled={bump}
              onCleared={() => {
                setSelectedId(null);
                bump();
              }}
            />
          )}
        </div>
      </main>

      {selectedId && (
        <JobDetail
          jobId={selectedId}
          onClose={() => setSelectedId(null)}
          onChanged={bump}
          onRerun={(copy) => setSelectedId(copy.id)}
        />
      )}
    </div>
  );
}
