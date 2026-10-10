import { Link } from "react-router";

import { Empty, PageHeader } from "../components/ui";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Страница не найдена" />
      <Empty>
        <Link to="/projects">К списку проектов</Link>
      </Empty>
    </>
  );
}
