const axios = require("axios");
const { post, put: update } = require("axios");
const CancelToken = require("axios").CancelToken;

post("/a", { data: 1 }, { proxy: false });
update("/b");
module.exports = { source: CancelToken.source(), all: axios.all([]) };
