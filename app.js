const express = require("express");
const app = express()
const userRoutes = require("./src/Routes/user.routes");
const errorHandler = require("./src/Middleware/error.middleware");

app.use(express.json());
app.use("/api", userRoutes);

app.use(errorHandler);


module.exports = app;
