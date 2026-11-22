import axios, { AxiosError as HttpError, type AxiosInstance } from "axios";
import * as qs from "qs";

export const client: AxiosInstance = axios.create({ baseURL: "/api", timeout: 5000 });

export async function load(id: string, options = {}) {
  try {
    const res = await axios.get(`/items/${id}`, { ...options, withCredentials: true });
    return res.data;
  } catch (err) {
    if (err instanceof HttpError) {
      return null;
    }
    throw err;
  }
}

export { CancelToken, isCancel as wasCancelled } from "axios";
