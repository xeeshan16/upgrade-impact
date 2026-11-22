import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { wasCancelled } from "../api";

const queryClient = new QueryClient({ defaultOptions: { queries: { cacheTime: 1000 } } });

export function Items() {
  const { data } = useQuery({ queryKey: ["items"], queryFn: load, cacheTime: 5 });
  queryClient.invalidateQueries({ queryKey: ["items"] });
  return wasCancelled(data) ? null : <ul>{data}</ul>;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient} {...rest}>
      <Items />
    </QueryClientProvider>
  );
}
