const express = require("express");
const router = express()
const userController = require("../Controllers/user.controller")

router.get("/users", userController.getUser)
router.get("/user/:id", userController.getUserById)
router.get("/userEmail/:email", userController.getUserByEmail)
router.post("/create", userController.createUser)
router.put("/update/:id", userController.updateUser)
router.delete("/delete/:id", userController.deleteUser)


module.exports = router