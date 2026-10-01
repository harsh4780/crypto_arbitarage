const userServices = require("../Services/user.services.js")


const getUser = async (req, res, next) => {
    try {

        const users = await userServices.getUser()
        res.status(200).json({
            success: true,
            data: users
        })

    } catch (error) {
        next(error)
    }
}

const getUserById = async (req, res, next) => {
    try {
        const user = await userServices.getUserById(req.params.id);
        res.status(200).json({
            success: true,
            data: user
        })
    } catch (error) {
        next(error)
    }

}

const getUserByEmail = async (req, res, next) => {
    try {
        const user = await userServices.getUserByEmail(req.params.email);
        res.status(200).json({
            success: true,
            data: user
        })
    } catch (error) {
        next(error)
    }

}

const createUser = (req, res, next) => {
    try {
        const { name, email } = req.body;
        console.log(name , email)
        
        const user = userServices.createUser(name, email)

        res.status(201).json({
            success: true,
            message: "User created successfully",
            data: user
        })
    } catch (error) {
        next(error)
    }
}


const updateUser = (req, res, next) => {
    try {
        const id = req.params.id;
        const data = req.body;
        const user = userServices.updatUser(id, data);

        res.status(200).json({
            success: true,
            message: "User updated successfully",
            data: user
        })
    } catch (error) {
        next(error)
    }
}

const deleteUser = (req, res, next) => {
    try {
        const id = req.params.id;
        const user = userServices.deleteUser(id);

        res.status(200).json({
            success: true,
            message: "User deleted successfully",
        })
    } catch (error) {
        next(error)
    }
}

module.exports = {
    getUser,
    getUserById,
    getUserByEmail,
    createUser,
    updateUser,
    deleteUser
}