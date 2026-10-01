const userRepository = require("../Repository/user.repository")


const getUser = async () => {
    return await userRepository.findAll();
}


const getUserById = async (id) => {
    const user = await userRepository.findById(id);

    if (!user) {
        const eroor = new Error("user not found")
        eroor.statusCode = 404;
        eroor.status = 'fail';
        throw eroor;
    }

    return user
}

const getUserByEmail = async (email) => {
    const user = await userRepository.findEmail(email)

    if (!user) {
        const eroor = new Error("user not found")
        eroor.statusCode = 404;
        eroor.status = 'fail';
        throw eroor;
    }

    return user
}


const createUser = async (name, email) => {

    console.log("email")
    const user = await userRepository.findEmail(email)

    if (user) {
        const error = new Error("user alredy exitst")
        error.statusCode = 409;
        throw error;
    }

    return userRepository.createUser({ name, email })
}


const updatUser = async (id, data) => {
    const user = await userRepository.findById(id);

    if (!user) {
        const eroor = new Error("user not found")
        eroor.statusCode = 404;
        eroor.status = 'fail';
        throw eroor;
    }

    return userRepository.update(id, data);

}

const deleteUser = async (id) => {
    const user = await userRepository.findById(id);

    if (!user) {
        const eroor = new Error("user not found")
        eroor.statusCode = 404;
        eroor.status = 'fail';
        throw eroor;
    }

    return await userRepository.deleteUser(id);
}

module.exports = {

    getUser,
    getUserByEmail,
    getUserById,
    createUser,
    updatUser,
    deleteUser

}